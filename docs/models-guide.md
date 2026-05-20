# Models & Fields Guide

Comprehensive guide to defining data models, field types, relationships, inheritance, and model-level features in HyperDjango.

---

## Table of Contents

- [Defining Models](#defining-models)
- [Field Types and Options](#field-types-and-options)
- [Relationships](#relationships)
- [Model Inheritance](#model-inheritance)
- [Meta Options](#meta-options)
- [Model Methods and Properties](#model-methods-and-properties)
- [Managers and Custom QuerySets](#managers-and-custom-querysets)
- [Signals](#signals)
- [Mixins](#mixins)
- [File and Image Fields](#file-and-image-fields)
- [Vector Fields (pgvector)](#vector-fields-pgvector)
- [Configuration Reference](#configuration-reference)
- [Migration Notes for Django Users](#migration-notes-for-django-users)

---

## Defining Models

A model is a Python class that maps to a PostgreSQL table. Fields use type annotations with the `Field()` descriptor for database metadata.

```python
from datetime import datetime
from hyperdjango.models import Field, Model


class Product(Model):
    class Meta:
        table = "products"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field()
    sku: str = Field(unique=True, index=True)
    price: float = Field(default=0.0)
    stock: int = Field(default=0)
    description: str = Field(default="")
    is_active: bool = Field(default=True)
    weight_kg: float | None = Field(default=None)
    created_at: datetime | None = Field(default=None)
    updated_at: datetime | None = Field(default=None)
```

Every model must define a `Meta` inner class with at minimum the `table` attribute specifying the PostgreSQL table name.

---

## Field Types and Options

### Supported Python Types

| Python Type   | PostgreSQL Type            | Notes                                                  |
| ------------- | -------------------------- | ------------------------------------------------------ |
| `int`         | `INTEGER` / `SERIAL`       | `auto=True` makes it `SERIAL`                          |
| `float`       | `DOUBLE PRECISION`         |                                                        |
| `str`         | `TEXT`                     | No `max_length` needed -- PostgreSQL TEXT is efficient |
| `bool`        | `BOOLEAN`                  |                                                        |
| `datetime`    | `TIMESTAMP WITH TIME ZONE` |                                                        |
| `date`        | `DATE`                     | Import from `datetime`                                 |
| `Decimal`     | `NUMERIC`                  | Import from `decimal`                                  |
| `uuid.UUID`   | `UUID`                     | Import from `uuid`                                     |
| `bytes`       | `BYTEA`                    |                                                        |
| `list[float]` | `vector(N)`                | Via `VectorField()` for pgvector                       |
| `dict`        | `JSONB`                    | Native JSON support                                    |

### Field Options

```python
from hyperdjango.models import Field

id: int = Field(primary_key=True, auto=True)  # Primary key with auto-increment
email: str = Field(unique=True)  # UNIQUE constraint
slug: str = Field(unique=True, index=True)  # UNIQUE + B-tree index
name: str = Field()  # Required field, no default
bio: str = Field(default="")  # Default value
score: int = Field(default=0, ge=0, le=100)  # With validation constraints
price: float = Field(ge=0.0)  # Non-negative
status: str = Field(default="draft")  # String with default
archived_at: datetime | None = Field(default=None)  # Nullable
```

### Field() Parameters

| Parameter      | Type        | Description                           |
| -------------- | ----------- | ------------------------------------- |
| `default`      | any         | Default value for the field           |
| `primary_key`  | `bool`      | Marks this as the primary key         |
| `auto`         | `bool`      | Auto-increment (SERIAL)               |
| `unique`       | `bool`      | UNIQUE constraint                     |
| `index`        | `bool`      | Create a B-tree index                 |
| `foreign_key`  | `str`       | Table name for FK constraint          |
| `related_name` | `str`       | Reverse relation name on target model |
| `ge`           | `int/float` | Minimum value (validation)            |
| `le`           | `int/float` | Maximum value (validation)            |
| `min_length`   | `int`       | Minimum string length (validation)    |
| `max_length`   | `int`       | Maximum string length (validation)    |
| `regex`        | `str`       | Regex pattern for validation          |

---

## Relationships

### Foreign Keys

```python
class Order(Model):
    class Meta:
        table = "orders"

    id: int = Field(primary_key=True, auto=True)
    customer_id: int = Field(foreign_key=Customer)
    product_id: int = Field(foreign_key=Product)
    quantity: int = Field(default=1)
    total: float = Field(default=0.0)
    created_at: datetime | None = Field(default=None)
```

Foreign keys reference the target model class. The column stores the integer FK value. Use `select_related()` at query time to JOIN and load the related object.

```python
orders = await Order.objects.select_related("customer", "product").all()
for order in orders:
    print(order.customer.name, order.product.name, order.total)
```

### Nested Foreign Keys

```python
# Book -> Author -> Publisher
books = await Book.objects.select_related("author__publisher").all()
for book in books:
    print(book.title, book.author.name, book.author.publisher.name)
```

### Many-to-Many

```python
from typing import ClassVar
from hyperdjango.models import Field, ManyToManyField, Model


class Article(Model):
    class Meta:
        table = "articles"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field()
    tags: ClassVar = ManyToManyField("tags")


class Tag(Model):
    class Meta:
        table = "tags"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(unique=True)
```

`ManyToManyField("tags")` automatically creates a junction table (`articles_tags`) with `article_id` and `tag_id` columns.

```python
# Prefetch M2M in batch (1 extra query total, not N+1)
articles = await Article.objects.prefetch_related("tags").all()
for article in articles:
    print(article.title, [t.name for t in article.tags])
```

### One-to-One

Model a one-to-one relationship with a unique foreign key:

```python
class UserProfile(Model):
    class Meta:
        table = "user_profiles"

    id: int = Field(primary_key=True, auto=True)
    user_id: int = Field(foreign_key=User, unique=True)
    avatar_url: str = Field(default="")
    timezone: str = Field(default="UTC")
    theme: str = Field(default="light")
```

---

## Model Inheritance

### Abstract Models

Abstract models share fields across multiple concrete models without creating their own table.

```python
class BaseContent(Model):
    class Meta:
        abstract = True

    id: int = Field(primary_key=True, auto=True)
    title: str = Field()
    slug: str = Field(unique=True, index=True)
    created_at: datetime | None = Field(default=None)
    updated_at: datetime | None = Field(default=None)


class BlogPost(BaseContent):
    class Meta:
        table = "blog_posts"

    body: str = Field(default="")
    author_id: int = Field(foreign_key=Author)


class NewsArticle(BaseContent):
    class Meta:
        table = "news_articles"

    summary: str = Field(default="")
    source_url: str = Field(default="")
```

Both `BlogPost` and `NewsArticle` inherit `id`, `title`, `slug`, `created_at`, and `updated_at` from `BaseContent`. Each maps to its own table.

### Proxy Models

Proxy models share the same table but provide different Python-level behavior or default queries.

```python
class PublishedPost(BlogPost):
    class Meta:
        proxy = True
        table = "blog_posts"

    # Custom manager that only returns published posts
    @classmethod
    def get_queryset(cls):
        return cls.objects.filter(published=True)
```

### Multi-Level Inheritance

```python
class TimestampedModel(Model):
    class Meta:
        abstract = True

    created_at: datetime | None = Field(default=None)
    updated_at: datetime | None = Field(default=None)


class SluggedModel(TimestampedModel):
    class Meta:
        abstract = True

    slug: str = Field(unique=True, index=True)


class Page(SluggedModel):
    class Meta:
        table = "pages"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field()
    body: str = Field(default="")
```

`Page` inherits `created_at`, `updated_at` from `TimestampedModel` and `slug` from `SluggedModel`.

---

## Meta Options

```python
class Product(Model):
    class Meta:
        table = "products"  # Required: PostgreSQL table name
        abstract = False  # True = no table, share fields only
        proxy = False  # True = same table, different Python class
        unlogged = False  # True = UNLOGGED table (no WAL, fast, lost on crash)
        cache_ttl = 60  # Query cache TTL in seconds (None = no cache)
        database = "default"  # Database alias for multi-db routing
```

| Option      | Type       | Default  | Description                                         |
| ----------- | ---------- | -------- | --------------------------------------------------- |
| `table`     | `str`      | required | PostgreSQL table name                               |
| `abstract`  | `bool`     | `False`  | Abstract base model (no table)                      |
| `proxy`     | `bool`     | `False`  | Proxy model (shared table)                          |
| `unlogged`  | `bool`     | `False`  | UNLOGGED table — no WAL, fast writes, lost on crash |
| `cache_ttl` | `int/None` | `None`   | Default query cache TTL in seconds                  |
| `database`  | `str/None` | `None`   | Database alias for multi-db routing                 |

---

## Model Methods and Properties

### Built-in Methods

```python
# Create
product = await Product.objects.create(name="Widget", sku="WDG-001", price=29.99)

# Save (INSERT or UPDATE depending on PK)
product.price = 34.99
await product.save()

# Delete
await product.delete()

# Refresh from database
await product.refresh_from_db()
```

### Custom Methods

```python
class Invoice(Model):
    class Meta:
        table = "invoices"

    id: int = Field(primary_key=True, auto=True)
    subtotal: float = Field(default=0.0)
    tax_rate: float = Field(default=0.0)
    discount: float = Field(default=0.0)
    status: str = Field(default="draft")

    @property
    def tax_amount(self) -> float:
        return self.subtotal * self.tax_rate

    @property
    def total(self) -> float:
        return self.subtotal + self.tax_amount - self.discount

    @property
    def is_paid(self) -> bool:
        return self.status == "paid"

    async def mark_paid(self):
        self.status = "paid"
        await self.save()
```

### DoesNotExist and MultipleObjectsReturned

Each model has auto-generated exception classes:

```python
try:
    user = await User.objects.get(id=999)
except User.DoesNotExist:
    print("No user with that ID")

try:
    user = await User.objects.get(is_active=True)
except User.MultipleObjectsReturned:
    print("Multiple active users found")
```

---

## Managers and Custom QuerySets

The default manager is `Model.objects`, which returns a `QuerySet`. Override to add custom query methods:

```python
class ActiveProductQuerySet:
    """Custom queryset methods for products."""

    @staticmethod
    def in_stock(qs):
        return qs.filter(stock__gt=0, is_active=True)

    @staticmethod
    def on_sale(qs):
        return qs.filter(sale_price__isnull=False, sale_price__gt=0)

    @staticmethod
    def by_category(qs, category_id: int):
        return qs.filter(category_id=category_id)


# Usage:
products = (
    await ActiveProductQuerySet.in_stock(Product.objects).order_by("-created_at").all()
)
```

---

## Signals

Signals allow decoupled notification when model events occur.

```python
from hyperdjango.signals import post_delete, post_save, pre_save


@pre_save.connect
async def set_timestamps(sender, instance, **kwargs):
    """Auto-set updated_at on every save."""
    from datetime import UTC, datetime

    instance.updated_at = datetime.now(UTC)


@post_save.connect
async def notify_new_order(sender, instance, created, **kwargs):
    """Send notification when a new order is created."""
    if sender.__name__ == "Order" and created:
        await send_order_notification(instance)


@post_delete.connect
async def cleanup_files(sender, instance, **kwargs):
    """Clean up uploaded files when a product is deleted."""
    if sender.__name__ == "Product" and instance.image:
        await storage.delete(instance.image)
```

### Available Signals

| Signal              | Arguments                      | When                 |
| ------------------- | ------------------------------ | -------------------- |
| `pre_save`          | `sender, instance`             | Before INSERT/UPDATE |
| `post_save`         | `sender, instance, created`    | After INSERT/UPDATE  |
| `pre_delete`        | `sender, instance`             | Before DELETE        |
| `post_delete`       | `sender, instance`             | After DELETE         |
| `user_logged_in`    | `sender, user, request`        | After login          |
| `user_logged_out`   | `sender, user, request`        | After logout         |
| `user_login_failed` | `sender, credentials, request` | After failed login   |

---

## Mixins

Pre-built mixins for common patterns:

```python
from hyperdjango.mixins import (
    OwnershipMixin,
    SoftDeleteMixin,
    TimestampMixin,
    VersionedMixin,
)


class Document(TimestampMixin, SoftDeleteMixin, VersionedMixin, Model):
    class Meta:
        table = "documents"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field()
    body: str = Field(default="")
```

| Mixin             | Fields Added               | Behavior                                         |
| ----------------- | -------------------------- | ------------------------------------------------ |
| `TimestampMixin`  | `created_at`, `updated_at` | Auto-set on save                                 |
| `SoftDeleteMixin` | `deleted_at`               | `delete()` sets timestamp, `restore()` clears it |
| `OwnershipMixin`  | `owner_id`                 | Auto-filter queries by owner                     |
| `VersionedMixin`  | `version`                  | Optimistic locking, auto-increment on save       |

### SoftDeleteMixin in Practice

```python
class Task(SoftDeleteMixin, Model):
    class Meta:
        table = "tasks"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field()
    completed: bool = Field(default=False)


# Soft delete (sets deleted_at, not actual DELETE)
await task.delete()

# Query automatically excludes soft-deleted rows
active_tasks = await Task.objects.all()

# Include soft-deleted rows
all_tasks = await Task.objects.unscoped().all()

# Restore a soft-deleted record
await task.restore()
```

---

## File and Image Fields

```python
from hyperdjango.models import Field, FileField, ImageField, Model


class Product(Model):
    class Meta:
        table = "products"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field()
    manual: str = FileField(upload_to="manuals/")
    photo: str = ImageField(upload_to="products/photos/")
```

File fields store the relative path in the database. Upload handling:

```python
from hyperdjango.models import save_uploaded_file
from hyperdjango.storage import FileSystemStorage

storage = FileSystemStorage(root="media/")

path = await save_uploaded_file(product, "photo", file_content, "product.jpg", storage)
await Product.objects.filter(id=product.id).update(photo=path)
```

---

## Vector Fields (pgvector)

For AI/ML applications with similarity search:

```python
from hyperdjango.models import Field, Model, VectorField


class Document(Model):
    class Meta:
        table = "documents"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field()
    content: str = Field(default="")
    embedding: list[float] = VectorField(
        dimensions=1536,  # OpenAI ada-002
        index_type="hnsw",  # or "ivfflat"
        index_ops="vector_cosine_ops",  # cosine distance
    )
```

Query by similarity:

```python
query_embedding = await get_embedding("search query")
similar_docs = (
    await Document.objects.filter(
        embedding__cosine_distance=(query_embedding, 0.3)  # max distance 0.3
    )
    .limit(10)
    .all()
)
```

---

## Configuration Reference

### Environment Variables

| Variable             | Default   | Description                  |
| -------------------- | --------- | ---------------------------- |
| `HYPER_DATABASE_URL` | None      | PostgreSQL connection string |
| `HYPER_DEBUG`        | `"false"` | Enable debug mode            |
| `HYPER_SECRET_KEY`   | None      | Application secret key       |

### Database Pool Settings

| Setting                            | Default    | Description                                            |
| ---------------------------------- | ---------- | ------------------------------------------------------ |
| `HYPERDJANGO_POOL_SIZE`            | `0` (auto) | Connection pool size (0 = THREAD_POOL_SIZE + headroom) |
| `HYPERDJANGO_PREPARED_STATEMENTS`  | `True`     | Enable prepared statement caching                      |
| `HYPERDJANGO_POOL_MAX_QUERIES`     | `10000`    | Rotate connections after N queries                     |
| `HYPERDJANGO_POOL_MAX_LIFETIME`    | `3600`     | Max connection age in seconds                          |
| `HYPERDJANGO_STATEMENT_CACHE_SIZE` | `256`      | LRU prepared statement cache size                      |
| `HYPERDJANGO_CONNECT_TIMEOUT`      | `10000`    | TCP connect timeout in ms                              |
| `HYPERDJANGO_QUERY_TIMEOUT`        | `0`        | Statement timeout in ms (0 = unlimited)                |

---

## Migration Notes for Django Users

### What Changed

| Django                                        | HyperDjango                                  | Rationale                                  |
| --------------------------------------------- | -------------------------------------------- | ------------------------------------------ |
| `models.CharField(max_length=200)`            | `name: str = Field()`                        | PostgreSQL TEXT has no performance penalty |
| `models.ForeignKey(Model, on_delete=CASCADE)` | `author_id: int = Field(foreign_key=Author)` | Reference model classes directly           |
| `models.ManyToManyField(Model)`               | `tags: ClassVar = ManyToManyField("table")`  | ClassVar annotation required               |
| `objects = MyManager()`                       | Static methods on separate class             | No custom manager metaclass                |
| `class Meta: ordering = ["-created_at"]`      | Use `.order_by()` at query time              | Explicit per-query ordering                |
| `objects.all()` (sync)                        | `await Model.objects.all()` (async)          | All DB ops are async                       |
| `PBKDF2` password hashing                     | `argon2id`                                   | Memory-hard, GPU-resistant                 |
| `django.db.models.signals`                    | `hyperdjango.signals`                        | Same signal names, async receivers         |
| `ModelManager`                                | `QuerySet` directly on `Model.objects`       | Simpler, no manager indirection            |

### What Stayed the Same

- Model definition is declarative classes with fields
- `Meta` inner class for table-level configuration
- `objects` attribute for querying
- `save()` / `delete()` instance methods
- `DoesNotExist` / `MultipleObjectsReturned` exceptions
- Signal names: `pre_save`, `post_save`, `pre_delete`, `post_delete`
- Abstract and proxy inheritance patterns

---

## Custom Model Fields

Create new field types by subclassing `Field` and overriding the conversion methods. This lets you define fields that store data in a specific PostgreSQL column type and convert between Python objects and database values.

### Field Lifecycle

When data flows between Python and PostgreSQL, three methods control the conversion:

| Method                 | Direction     | Purpose                                                                     |
| ---------------------- | ------------- | --------------------------------------------------------------------------- |
| `db_type()`            | Python -> DDL | Returns the PostgreSQL column type for CREATE TABLE                         |
| `to_db_value(value)`   | Python -> SQL | Converts a Python value to a database-compatible value before INSERT/UPDATE |
| `from_db_value(value)` | SQL -> Python | Converts a raw database value to a Python object after SELECT               |

### Example: Custom JSONField

A field that stores Python dicts/lists as JSONB in PostgreSQL, with automatic serialization.

```python
import json
from hyperdjango.models import Field


class JSONField(Field):
    """Store arbitrary JSON-serializable data in a JSONB column.

    Usage:
        class Product(Model):
            class Meta:
                table = "products"

            id: int = Field(primary_key=True, auto=True)
            metadata: dict = JSONField(default_factory=dict)
            tags: list = JSONField(default_factory=list)
    """

    def __init__(self, *, default=None, default_factory=None, **kwargs):
        if default_factory is not None:
            kwargs["default"] = default_factory
        elif default is not None:
            kwargs["default"] = default
        super().__init__(**kwargs)
        self._default_factory = default_factory

    def db_type(self) -> str:
        """PostgreSQL column type."""
        return "JSONB"

    def to_db_value(self, value):
        """Serialize Python object to JSON string for storage."""
        if value is None:
            return None
        return json.dumps(value)

    def from_db_value(self, value):
        """Deserialize JSONB from database to Python object."""
        if value is None:
            return self._default_factory() if self._default_factory else None
        if isinstance(value, str):
            return json.loads(value)
        return value  # pg.zig returns dicts directly for JSONB
```

Usage with a model:

```python
class Product(Model):
    class Meta:
        table = "products"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field()
    attributes: dict = JSONField(default_factory=dict)
    tags: list = JSONField(default_factory=list)


# Create with nested data
product = await Product.objects.create(
    name="Laptop",
    attributes={"cpu": "M4", "ram_gb": 32, "ports": ["usb-c", "hdmi"]},
    tags=["electronics", "computers", "sale"],
)

# Query with JSONB operators (via raw SQL or lookups)
rows = await Product.objects.filter(attributes__contains={"cpu": "M4"}).all()
```

### Example: ArrayField

A field that maps to PostgreSQL's native array types.

```python
from hyperdjango.models import Field


class ArrayField(Field):
    """Store a list of values in a PostgreSQL array column.

    Usage:
        class Article(Model):
            class Meta:
                table = "articles"

            id: int = Field(primary_key=True, auto=True)
            categories: list[str] = ArrayField(base_type="TEXT")
            scores: list[int] = ArrayField(base_type="INTEGER")
    """

    def __init__(self, *, base_type: str = "TEXT", **kwargs):
        super().__init__(**kwargs)
        self.base_type = base_type

    def db_type(self) -> str:
        return f"{self.base_type}[]"

    def to_db_value(self, value):
        if value is None:
            return None
        return list(value)

    def from_db_value(self, value):
        if value is None:
            return []
        return list(value)
```

### Example: MoneyField

A field with built-in precision handling for financial amounts.

```python
from decimal import Decimal
from hyperdjango.models import Field


class MoneyField(Field):
    """Store monetary values as NUMERIC(12, 2) with Decimal conversion.

    Prevents floating-point precision issues for financial calculations.

    Usage:
        class Invoice(Model):
            class Meta:
                table = "invoices"

            id: int = Field(primary_key=True, auto=True)
            subtotal: Decimal = MoneyField()
            tax: Decimal = MoneyField()
    """

    def __init__(self, *, precision: int = 12, scale: int = 2, **kwargs):
        super().__init__(**kwargs)
        self.precision = precision
        self.scale = scale

    def db_type(self) -> str:
        return f"NUMERIC({self.precision}, {self.scale})"

    def to_db_value(self, value):
        if value is None:
            return None
        return str(Decimal(str(value)).quantize(Decimal(f"0.{'0' * self.scale}")))

    def from_db_value(self, value):
        if value is None:
            return Decimal("0.00")
        return Decimal(str(value))
```

### Registering Custom Fields for Migrations

If your custom field has a `db_type()` method, the migration system will use
it automatically when generating CREATE TABLE statements. No additional
registration is needed.
