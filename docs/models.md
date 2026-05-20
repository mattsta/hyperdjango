# Models & ORM

Define your data schema as Python classes. HyperDjango models map to PostgreSQL tables with a high-performance async ORM backed by the native pg.zig driver.

All ORM operations are async. Every query flows through the pg.zig native PostgreSQL driver — there is no Python-level database adapter. Queries use parameterized `$1, $2, ...` placeholders and prepared statement caching for maximum throughput.

---

## Table of Contents

- [Defining Models](#defining-models)
- [Field Types](#field-types)
- [Field Options](#field-options)
- [Relationships](#relationships)
- [QuerySet API — Methods Returning New QuerySets](#queryset-api-methods-returning-new-querysets)
- [QuerySet API — Methods That Evaluate](#queryset-api-methods-that-evaluate)
- [Field Lookups](#field-lookups)
- [Aggregation Functions](#aggregation-functions)
- [Q Objects](#q-objects)
- [F Expressions](#f-expressions)
- [Raw SQL](#raw-sql)
- [Transactions](#transactions)
- [Model Inheritance](#model-inheritance)
- [Model Mixins](#model-mixins)
- [Model Instance Methods](#model-instance-methods)
- [Composite Primary Keys](#composite-primary-keys)
- [Meta Options](#meta-options)
- [Database Functions](#database-functions)
- [Query Cache](#query-cache)
- [Model Signals](#model-signals)
- [Performance](#performance)

---

## Defining Models

```python
from hyperdjango import Model, Field


class Article(Model):
    class Meta:
        table = "articles"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field()
    content: str = Field(default="")
    published: bool = Field(default=False)
    views: int = Field(default=0)
    author_id: int = Field(foreign_key=User)
```

Every model needs:

- A `Meta` inner class with `table` — the PostgreSQL table name
- At least one primary key field (usually `id: int = Field(primary_key=True, auto=True)`)
- Type-annotated fields with `Field()` descriptors

---

## Field Types

HyperDjango uses Python type annotations for field types. The ORM maps them to PostgreSQL column types:

| Python Type | PostgreSQL Type      | Example                                              |
| ----------- | -------------------- | ---------------------------------------------------- |
| `int`       | `INTEGER` / `SERIAL` | `id: int = Field(primary_key=True, auto=True)`       |
| `str`       | `TEXT`               | `name: str = Field()`                                |
| `bool`      | `BOOLEAN`            | `active: bool = Field(default=True)`                 |
| `float`     | `DOUBLE PRECISION`   | `price: float = Field(default=0.0)`                  |
| `datetime`  | `TIMESTAMPTZ`        | `created: datetime = Field(default=None)`            |
| `date`      | `DATE`               | `birthday: date = Field(default=None)`               |
| `time`      | `TIME`               | `alarm: time = Field(default=None)`                  |
| `Decimal`   | `NUMERIC`            | `amount: Decimal = Field(default=Decimal("0"))`      |
| `uuid.UUID` | `UUID`               | `ref: UUID = Field(default=None)`                    |
| `bytes`     | `BYTEA`              | `data: bytes = Field(default=b"")`                   |
| `dict`      | `JSONB`              | `meta: dict[str, str] = Field(default_factory=dict)` |
| `list`      | `JSONB`              | `tags: list[str] = Field(default_factory=list)`      |

---

## Field Options

```python
Field(
    default=None,  # Python-level default value (or _MISSING for required)
    db_default=None,  # Database-level DEFAULT (literal or DatabaseDefault)
    primary_key=False,  # This is the primary key
    auto=False,  # Auto-increment (SERIAL, or BIGSERIAL with big=True)
    big=False,  # 64-bit width: BIGSERIAL auto-PK / BIGINT int/FK column
    db_type=None,  # Explicit SQL column type override (e.g. "BIGINT")
    unique=False,  # UNIQUE constraint
    index=False,  # Create an index
    foreign_key=None,  # Model class for FK (e.g., User)
    related_name=None,  # Reverse relation name
    one_to_one=False,  # OneToOneField (unique FK)
    ge=None,  # Greater than or equal (validation)
    le=None,  # Less than or equal (validation)
    gt=None,  # Greater than (validation)
    lt=None,  # Less than (validation)
    min_length=None,  # Minimum string length
    pattern=None,  # Regex pattern validation
)
```

### `db_default` — database-evaluated defaults

`default` is applied in Python when you construct an instance; `db_default`
emits a SQL `DEFAULT` clause that **PostgreSQL** evaluates on `INSERT`. Use it
when the value should be computed by the database — including for primary keys.

```python
import uuid
from hyperdjango import DatabaseDefault
from hyperdjango.models import Model, Field


class Event(Model):
    # Server-generated UUID primary key
    id: uuid.UUID = Field(
        primary_key=True, db_default=DatabaseDefault("gen_random_uuid()")
    )
    # Literal default (quoted/escaped automatically) → DEFAULT 'pending'
    status: str = Field(db_default="pending")
    # Raw SQL expression → DEFAULT now()
    seen_at: datetime = Field(db_default=DatabaseDefault("now()"))
```

- A literal (`"pending"`, `0`, `True`) is SQL-quoted/escaped for you;
  a `DatabaseDefault("…")` is emitted verbatim as a SQL expression.
- When you `save()` an instance without setting a `db_default` column, the
  column is omitted from the `INSERT` so PostgreSQL fills it; the generated
  value (e.g. a UUID primary key) is read back via `RETURNING` and assigned to
  the instance. Passing an explicit value overrides the database default.

### `big` — 64-bit integer columns

By default an `int` column is a 32-bit `INTEGER` (and an `auto=True` primary key
is a `SERIAL`), whose identity/value ceiling is ~2.1 billion. `Field(big=True)`
widens it to 64-bit — essential for high-volume append/cursor tables and for
storing values that exceed the int32 range (e.g. H3 cell indexes, Snowflake IDs):

```python
class Event(Model):
    class Meta:
        table = "events"

    # BIGSERIAL — 64-bit auto-increment primary key
    id: int = Field(primary_key=True, auto=True, big=True)
    # BIGINT — plain 64-bit integer column
    sequence: int = Field(big=True, index=True)
```

| Field                                | Without `big`       | With `big=True` |
| ------------------------------------ | ------------------- | --------------- |
| `Field(primary_key=True, auto=True)` | `SERIAL`            | `BIGSERIAL`     |
| `Field()` on an `int` column         | `INTEGER`           | `BIGINT`        |
| `Field(foreign_key=…)`               | derived (see below) | `BIGINT`        |

`big` is additive: a plain field is byte-for-byte unchanged (`SERIAL`/`INTEGER`).

### `db_type` — explicit column-type override

When the SQL type you want can't be expressed by the Python annotation, set it
directly with `db_type`. The override wins over annotation inference and over
`big`:

```python
class Ledger(Model):
    class Meta:
        table = "ledger"

    id: int = Field(primary_key=True, auto=True, big=True)
    # Force a specific numeric type the annotation can't express
    balance: int = Field(db_type="NUMERIC(20, 4)")
    # Postgres money, tsvector, custom domains, etc.
    amount: int = Field(db_type="money")
```

On an `auto=True` primary key, an explicit `db_type` of `BIGINT`/`BIGSERIAL`
resolves to `BIGSERIAL`; any other value is used verbatim.

### Optional / nullable annotations

A column is generated `NOT NULL` unless its annotation admits `None`. HyperDjango
resolves both the column **type** and its **nullability** from every common
optional form — a real `X | None` (PEP 604) union, `typing.Optional[X]` /
`typing.Union[X, None]`, and the **string** annotations produced by
`from __future__ import annotations` (PEP 563), e.g. `"datetime | None"`:

```python
from __future__ import annotations  # annotations are strings at runtime


class Session(Model):
    class Meta:
        table = "sessions"

    id: int = Field(primary_key=True, auto=True)
    token: str = Field()  # TEXT NOT NULL
    expires_at: datetime | None = Field()  # TIMESTAMPTZ (nullable)
    payload: dict | None = Field()  # JSONB (nullable)
```

Both the base type (`TIMESTAMPTZ`, `JSONB`, …) and the nullability are derived
from the annotation, so a model-derived schema (`hyper setup`) matches
hand-written migrations for optional columns.

---

## Meta.indexes

Define composite, partial, GIN, and expression indexes declaratively on the Model:

```python
from hyperdjango.models import Index


class Ticket(TenantMixin, TimestampMixin, Model):
    class Meta:
        table = "tickets"
        indexes = [
            # Composite index
            Index(fields=("tenant_id", "status_id")),
            # DESC ordering
            Index(fields=("tenant_id", "-created_at")),
            # Unique constraint
            Index(fields=("tenant_id", "ticket_number"), unique=True),
            # Partial index (WHERE clause)
            Index(fields=("tenant_id", "assignee_id"), where="is_deleted = FALSE"),
            # GIN full-text search (expression index)
            Index(
                expressions=("(to_tsvector('english', title || ' ' || description))",),
                using="gin",
                name="ix_tickets_search",
            ),
            # GIN trigram (requires pg_trgm extension)
            Index(fields=("title",), using="gin", opclasses=("gin_trgm_ops",)),
            # HNSW vector index
            Index(
                fields=("embedding",),
                using="hnsw",
                opclasses=("vector_cosine_ops",),
                params={"m": 16, "ef_construction": 64},
            ),
        ]
```

### Index Options

| Option        | Type              | Default   | Description                                                    |
| ------------- | ----------------- | --------- | -------------------------------------------------------------- |
| `fields`      | `tuple[str, ...]` | `()`      | Column names. Prefix with `-` for DESC.                        |
| `name`        | `str \| None`     | auto      | Index name. Auto: `idx_{table}_{cols}` or `uq_{table}_{cols}`. |
| `unique`      | `bool`            | `False`   | UNIQUE constraint.                                             |
| `using`       | `str`             | `"btree"` | Index type: `btree`, `gin`, `gist`, `hnsw`, `ivfflat`.         |
| `where`       | `str \| None`     | `None`    | Partial index WHERE clause (raw SQL).                          |
| `opclasses`   | `tuple[str, ...]` | `()`      | Per-column operator classes (e.g., `gin_trgm_ops`).            |
| `include`     | `tuple[str, ...]` | `()`      | INCLUDE columns (covering index).                              |
| `params`      | `dict`            | `None`    | WITH parameters (e.g., `{"m": 16}`).                           |
| `expressions` | `tuple[str, ...]` | `()`      | Raw SQL expressions for functional indexes.                    |

Index names are auto-generated as `idx_{table}_{col1}_{col2}` (or `uq_` for unique), truncated to 63 characters. Override with explicit `name=`.

`Field(index=True)` still works for simple single-column B-tree indexes. Use `Meta.indexes` for composite, partial, GIN, expression, and other advanced index types.

All indexes are created by `hyper setup` via `generate_ddl_for_model()`.

---

## Relationships

### Foreign Keys

```python
class Author(Model):
    class Meta:
        table = "authors"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field()


class Article(Model):
    class Meta:
        table = "articles"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field()
    author_id: int = Field(foreign_key=Author)


# Query across FK
articles = await Article.objects.select_related("author").all()
for article in articles:
    print(article.author.name)  # resolved via JOIN
```

The foreign-key column's SQL type is **derived from the target model's primary
key**, so it always matches: a FK to a `BIGSERIAL`/`big=True` PK becomes `BIGINT`,
a FK to a `TEXT` or `UUID` PK uses that type, and a FK to a plain `SERIAL` PK
stays `INTEGER`. Override it explicitly with `Field(foreign_key=…, big=True)` or
`Field(foreign_key=…, db_type="BIGINT")` when you need to force the width (for
example, an FK declared before its target is importable).

### Many-to-Many

```python
from hyperdjango.models import ManyToManyField


class Tag(Model):
    class Meta:
        table = "tags"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field()


class Article(Model):
    class Meta:
        table = "articles"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field()
    tags: ClassVar = ManyToManyField("tags")


# M2M operations
await article.tags.add(tag1, tag2)
await article.tags.remove(tag1)
all_tags = await article.tags.all()
await article.tags.clear()
await article.tags.set([tag1, tag2, tag3])
count = await article.tags.count()
```

### One-to-One

Use `OneToOneField` for relationships where each row in the source table maps to exactly one row in the target table (e.g., user profiles):

```python
from hyperdjango.models import Field, Model, OneToOneField


class User(Model):
    class Meta:
        table = "users"

    id: int = Field(primary_key=True, auto=True)
    username: str = Field(unique=True)


class Profile(Model):
    class Meta:
        table = "profiles"

    id: int = Field(primary_key=True, auto=True)
    user_id: int = OneToOneField("users", related_name="profile")
    bio: str = Field(default="")
    website: str = Field(default="")


# Query
profile = await Profile.objects.filter(user_id=user.id).first()

# With JOIN
profiles = await Profile.objects.select_related("user_id").all()
```

`OneToOneField` creates a foreign key column with a UNIQUE constraint — the database enforces that each target row is referenced at most once.

---

## QuerySet API — Methods Returning New QuerySets

These methods are chainable. They return a new `QuerySet` without hitting the database until you evaluate it (by calling `.all()`, iterating, etc.).

---

### `filter(**kwargs) -> QuerySet`

Returns a new QuerySet containing objects that match the given lookup parameters.

**Signature:**

```python
QuerySet.filter(**kwargs) -> QuerySet
```

**Parameters:**

| Parameter  | Type              | Description                                      |
| ---------- | ----------------- | ------------------------------------------------ |
| `**kwargs` | keyword arguments | Field lookups using `field__lookup=value` syntax |

**SQL generated:**

```sql
SELECT * FROM articles WHERE published = $1
```

**Example:**

```python
# Simple equality (exact is the default lookup)
published = await Article.objects.filter(published=True).all()

# With lookup operator
recent = await Article.objects.filter(views__gt=100).all()

# Multiple conditions (AND)
popular_published = await Article.objects.filter(published=True, views__gt=1000).all()

# Chaining (also AND)
same = await Article.objects.filter(published=True).filter(views__gt=1000).all()
```

**Return type:** `QuerySet`

Multiple keyword arguments are joined with `AND`. For `OR` conditions, use [Q objects](#q-objects).

---

### `exclude(**kwargs) -> QuerySet`

Returns a new QuerySet containing objects that do NOT match the given lookup parameters. The inverse of `filter()`.

**Signature:**

```python
QuerySet.exclude(**kwargs) -> QuerySet
```

**Parameters:**

| Parameter  | Type              | Description             |
| ---------- | ----------------- | ----------------------- |
| `**kwargs` | keyword arguments | Field lookups to negate |

**SQL generated:**

```sql
SELECT * FROM articles WHERE NOT (published = $1)
```

**Example:**

```python
# Exclude unpublished
visible = await Article.objects.exclude(published=False).all()

# Exclude with lookups
not_empty = await Article.objects.exclude(content__isnull=True).all()

# Chain with filter
published_with_views = (
    await Article.objects.filter(published=True).exclude(views=0).all()
)
```

**Return type:** `QuerySet`

---

### `annotate(*args, **kwargs) -> QuerySet`

Adds computed fields to each object in the QuerySet using aggregation expressions.

**Signature:**

```python
QuerySet.annotate(*args, **kwargs) -> QuerySet
```

**Parameters:**

| Parameter  | Type                 | Description                                |
| ---------- | -------------------- | ------------------------------------------ |
| `**kwargs` | expression arguments | Named annotations: `alias=Expression(...)` |

**SQL generated:**

```sql
SELECT articles.*, COUNT(comments.id) AS comment_count
FROM articles
LEFT JOIN comments ON comments.article_id = articles.id
GROUP BY articles.id
```

**Example:**

```python
from hyperdjango.expressions import Count, Sum, Avg, F

# Count related objects
articles = await Article.objects.annotate(
    comment_count=Count("comments__id"),
).all()
for article in articles:
    print(article.comment_count)  # added attribute

# Sum across relations
authors = (
    await Author.objects.annotate(
        total_views=Sum("articles__views"),
    )
    .order_by("-total_views")
    .all()
)

# Annotate + filter on the annotation
popular = (
    await Article.objects.annotate(
        comment_count=Count("comments__id"),
    )
    .filter(comment_count__gt=10)
    .all()
)

# Multiple annotations
stats = await Article.objects.annotate(
    comment_count=Count("comments__id"),
    avg_rating=Avg("ratings__score"),
).all()
```

**Return type:** `QuerySet` (each object gains the annotated attribute)

---

### `order_by(*fields) -> QuerySet`

Returns a new QuerySet ordered by the given fields.

**Signature:**

```python
QuerySet.order_by(*fields) -> QuerySet
```

**Parameters:**

| Parameter | Type  | Description                                        |
| --------- | ----- | -------------------------------------------------- |
| `*fields` | `str` | Field names. Prefix with `-` for descending order. |

**SQL generated:**

```sql
SELECT * FROM articles ORDER BY views DESC, title ASC
```

**Example:**

```python
# Ascending (default)
await Article.objects.order_by("title").all()

# Descending (prefix with -)
await Article.objects.order_by("-views").all()

# Multiple columns
await Article.objects.order_by("-published", "title").all()

# Clear default ordering (if Meta.ordering is set)
await Article.objects.order_by().all()
```

**Return type:** `QuerySet`

---

### `reverse() -> QuerySet`

Reverses the ordering of the QuerySet. Only meaningful on an ordered QuerySet.

**Signature:**

```python
QuerySet.reverse() -> QuerySet
```

**Parameters:** None.

**SQL generated:**

```sql
-- If original order was ORDER BY views DESC:
SELECT * FROM articles ORDER BY views ASC
```

**Example:**

```python
# Get the last 5 articles by date
last_five = await Article.objects.order_by("pub_date").reverse().limit(5)
```

**Return type:** `QuerySet`

---

### `distinct(*fields) -> QuerySet`

Returns a new QuerySet that uses `SELECT DISTINCT` in its SQL query. On PostgreSQL, you can pass field names to use `DISTINCT ON`.

**Signature:**

```python
QuerySet.distinct(*fields) -> QuerySet
```

**Parameters:**

| Parameter | Type  | Description                                                                            |
| --------- | ----- | -------------------------------------------------------------------------------------- |
| `*fields` | `str` | (Optional) Field names for `DISTINCT ON`. If empty, applies `DISTINCT` to all columns. |

**SQL generated:**

```sql
-- No arguments:
SELECT DISTINCT * FROM articles

-- With field arguments (PostgreSQL DISTINCT ON):
SELECT DISTINCT ON (author_id) * FROM articles ORDER BY author_id, pub_date DESC
```

**Example:**

```python
# All distinct rows
unique_articles = await Article.objects.values("author_id").distinct().all()

# DISTINCT ON — one article per author (most recent)
one_per_author = (
    await Article.objects.distinct("author_id").order_by("author_id", "-pub_date").all()
)
```

**Return type:** `QuerySet`

---

### `values(*fields) -> QuerySet`

Returns a QuerySet that returns dictionaries instead of model instances, with only the specified fields.

**Signature:**

```python
QuerySet.values(*fields) -> QuerySet
```

**Parameters:**

| Parameter | Type  | Description                                                |
| --------- | ----- | ---------------------------------------------------------- |
| `*fields` | `str` | Field names to include. If empty, all fields are included. |

**SQL generated:**

```sql
SELECT title, views FROM articles
```

**Example:**

```python
# Specific columns as dicts
titles = await Article.objects.values("title", "views").all()
# [{"title": "Hello", "views": 42}, {"title": "World", "views": 99}]

# All columns as dicts
all_dicts = await Article.objects.values().all()

# Combined with filter
published_titles = await Article.objects.filter(published=True).values("title").all()
```

**Return type:** `QuerySet` (evaluates to `list[dict[str, ...]]`)

---

### `values_list(*fields, flat=False) -> QuerySet`

Like `values()` but returns tuples instead of dictionaries. With `flat=True` and a single field, returns a flat list.

**Signature:**

```python
QuerySet.values_list(*fields, flat=False) -> QuerySet
```

**Parameters:**

| Parameter | Type   | Description                                                           |
| --------- | ------ | --------------------------------------------------------------------- |
| `*fields` | `str`  | Field names to include                                                |
| `flat`    | `bool` | If `True` and exactly one field, return flat list instead of 1-tuples |

**SQL generated:**

```sql
SELECT title, views FROM articles
```

**Example:**

```python
# Tuples
pairs = await Article.objects.values_list("title", "views").all()
# [("Hello", 42), ("World", 99)]

# Flat list of single field
all_titles = await Article.objects.values_list("title", flat=True).all()
# ["Hello", "World"]

# All IDs
all_ids = await Article.objects.values_list("id", flat=True).all()
# [1, 2, 3, ...]
```

**Return type:** `QuerySet` (evaluates to `list[tuple[...]]` or `list[T]` when `flat=True`)

---

### `none() -> QuerySet`

Returns an empty QuerySet that never hits the database. Useful as a sentinel value or base for conditional query building.

**Signature:**

```python
QuerySet.none() -> QuerySet
```

**Parameters:** None.

**SQL generated:** No SQL is executed.

**Example:**

```python
# Returns empty list, no query
empty = await Article.objects.none().all()
# []

# Useful for conditional queries
if not user.is_authenticated:
    qs = Article.objects.none()
else:
    qs = Article.objects.filter(author_id=user.id)
results = await qs.all()
```

**Return type:** `QuerySet` (always evaluates to empty)

---

### `all() -> QuerySet`

Returns a copy of the current QuerySet. Often used to evaluate the QuerySet (triggering the database query) or to get a fresh copy of the manager's base QuerySet.

**Signature:**

```python
QuerySet.all() -> QuerySet
```

**Parameters:** None.

**SQL generated:**

```sql
SELECT * FROM articles
```

**Example:**

```python
# Get all objects
articles = await Article.objects.all()

# Clone a queryset
qs = Article.objects.filter(published=True)
qs_copy = qs.all()  # independent copy
```

**Return type:** `QuerySet`

---

### `union(*other_qs, all=False) -> QuerySet`

Combines results from two or more QuerySets using SQL `UNION`. By default, removes duplicates. Pass `all=True` for `UNION ALL`.

**Signature:**

```python
QuerySet.union(*other_qs, all=False) -> QuerySet
```

**Parameters:**

| Parameter   | Type       | Description                                  |
| ----------- | ---------- | -------------------------------------------- |
| `*other_qs` | `QuerySet` | One or more QuerySets to union with          |
| `all`       | `bool`     | If `True`, use `UNION ALL` (keep duplicates) |

**SQL generated:**

```sql
(SELECT * FROM articles WHERE published = true)
UNION
(SELECT * FROM articles WHERE views > 1000)
```

**Example:**

```python
qs1 = Article.objects.filter(published=True)
qs2 = Article.objects.filter(views__gt=1000)

# UNION (deduplicates)
combined = await qs1.union(qs2).all()

# UNION ALL (keeps duplicates)
combined_all = await qs1.union(qs2, all=True).all()
```

**Return type:** `QuerySet`

Only `LIMIT`, `OFFSET`, and `ORDER BY` are allowed after `union()`. You cannot call `filter()`, `exclude()`, etc. on the result.

---

### `intersection(*other_qs) -> QuerySet`

Returns only the rows that appear in ALL of the given QuerySets using SQL `INTERSECT`.

**Signature:**

```python
QuerySet.intersection(*other_qs) -> QuerySet
```

**Parameters:**

| Parameter   | Type       | Description                             |
| ----------- | ---------- | --------------------------------------- |
| `*other_qs` | `QuerySet` | One or more QuerySets to intersect with |

**SQL generated:**

```sql
(SELECT * FROM articles WHERE published = true)
INTERSECT
(SELECT * FROM articles WHERE views > 1000)
```

**Example:**

```python
published = Article.objects.filter(published=True)
popular = Article.objects.filter(views__gt=1000)

# Only published AND popular
both = await published.intersection(popular).all()
```

**Return type:** `QuerySet`

---

### `difference(*other_qs) -> QuerySet`

Returns rows from the first QuerySet that are NOT in any of the other QuerySets, using SQL `EXCEPT`.

**Signature:**

```python
QuerySet.difference(*other_qs) -> QuerySet
```

**Parameters:**

| Parameter   | Type       | Description                       |
| ----------- | ---------- | --------------------------------- |
| `*other_qs` | `QuerySet` | One or more QuerySets to subtract |

**SQL generated:**

```sql
(SELECT * FROM articles WHERE published = true)
EXCEPT
(SELECT * FROM articles WHERE views > 1000)
```

**Example:**

```python
published = Article.objects.filter(published=True)
popular = Article.objects.filter(views__gt=1000)

# Published but NOT popular
quiet = await published.difference(popular).all()
```

**Return type:** `QuerySet`

---

### `select_related(*fields) -> QuerySet`

Returns a QuerySet that follows foreign-key relationships via `LEFT JOIN`, loading related objects in the same database query. Eliminates N+1 queries for forward FK relationships.

**Signature:**

```python
QuerySet.select_related(*fields) -> QuerySet
```

**Parameters:**

| Parameter | Type  | Description                                                  |
| --------- | ----- | ------------------------------------------------------------ |
| `*fields` | `str` | FK field names to follow. Use `__` for nested relationships. |

**SQL generated:**

```sql
SELECT articles.*, authors.id AS author__id, authors.name AS author__name
FROM articles
LEFT JOIN authors ON articles.author_id = authors.id
```

**Example:**

```python
# Single FK — resolved via LEFT JOIN
articles = await Article.objects.select_related("author").all()
for a in articles:
    print(a.author.name)  # no extra query

# Nested FK (author -> publisher)
articles = await Article.objects.select_related("author__publisher").all()
for a in articles:
    print(a.author.publisher.name)  # all from one query

# Multiple FKs
articles = await Article.objects.select_related("author", "category").all()
```

**Return type:** `QuerySet`

Use `select_related` for foreign key and one-to-one relationships (forward direction). For reverse FK and many-to-many, use `prefetch_related`.

---

### `prefetch_related(*lookups) -> QuerySet`

Returns a QuerySet that prefetches reverse FK and many-to-many relationships in a separate query, then joins them in Python. Prevents N+1 queries for reverse and M2M relationships.

**Signature:**

```python
QuerySet.prefetch_related(*lookups) -> QuerySet
```

**Parameters:**

| Parameter  | Type  | Description               |
| ---------- | ----- | ------------------------- |
| `*lookups` | `str` | Related names to prefetch |

**SQL generated:**

```sql
-- First query: main objects
SELECT * FROM authors
-- Second query: related objects (executed separately)
SELECT * FROM articles WHERE author_id IN ($1, $2, $3, ...)
```

**Example:**

```python
# Reverse FK — resolved via separate query
authors = await Author.objects.prefetch_related("articles").all()
for author in authors:
    print(author.articles)  # pre-loaded, no N+1

# M2M
articles = await Article.objects.prefetch_related("tags").all()
for article in articles:
    print(article.tags)  # pre-loaded

# Multiple prefetches
authors = await Author.objects.prefetch_related("articles", "reviews").all()
```

**Return type:** `QuerySet`

Unlike `select_related` (which uses JOINs), `prefetch_related` executes a separate query for each prefetch and does the joining in Python. This is more efficient for one-to-many and many-to-many relationships.

---

### `defer(*fields) -> QuerySet`

Returns a QuerySet that defers loading of the specified fields. Deferred fields are not loaded from the database until accessed on the instance. Useful for excluding large text or binary columns.

**Signature:**

```python
QuerySet.defer(*fields) -> QuerySet
```

**Parameters:**

| Parameter | Type  | Description                                    |
| --------- | ----- | ---------------------------------------------- |
| `*fields` | `str` | Field names to exclude from the initial SELECT |

**SQL generated:**

```sql
SELECT id, title, published, views, author_id FROM articles
-- (content column omitted)
```

**Example:**

```python
# Defer large content column
articles = await Article.objects.defer("content").all()
for a in articles:
    print(a.title)  # loaded immediately
    print(a.content)  # triggers a separate query on access
```

**Return type:** `QuerySet`

You cannot defer the primary key field.

---

### `only(*fields) -> QuerySet`

The opposite of `defer()`. Returns a QuerySet that loads ONLY the specified fields immediately. All other fields are deferred.

**Signature:**

```python
QuerySet.only(*fields) -> QuerySet
```

**Parameters:**

| Parameter | Type  | Description                          |
| --------- | ----- | ------------------------------------ |
| `*fields` | `str` | Field names to include in the SELECT |

**SQL generated:**

```sql
SELECT id, title FROM articles
```

**Example:**

```python
# Only load id and title
articles = await Article.objects.only("id", "title").all()
for a in articles:
    print(a.title)  # loaded
    print(a.content)  # triggers separate query
```

**Return type:** `QuerySet`

The primary key is always included even if not specified.

---

### `using(alias) -> QuerySet`

Selects which database to use for the query. For multi-database setups.

**Signature:**

```python
QuerySet.using(alias) -> QuerySet
```

**Parameters:**

| Parameter | Type  | Description                                            |
| --------- | ----- | ------------------------------------------------------ |
| `alias`   | `str` | Database alias as configured in your database settings |

**SQL generated:** Same SQL, routed to a different database connection.

**Example:**

```python
# Read from replica
articles = await Article.objects.using("replica").filter(published=True).all()

# Write to primary
await Article.objects.using("primary").create(title="New Article")
```

**Return type:** `QuerySet`

See [Multi-Database](multi-db.md) for full multi-database routing documentation.

---

### `select_for_update(nowait=False, skip_locked=False, of=()) -> QuerySet`

Locks the selected rows until the end of the transaction. Maps to PostgreSQL `SELECT ... FOR UPDATE`.

**Signature:**

```python
QuerySet.select_for_update(nowait=False, skip_locked=False, of=()) -> QuerySet
```

**Parameters:**

| Parameter     | Type              | Description                                                                              |
| ------------- | ----------------- | ---------------------------------------------------------------------------------------- |
| `nowait`      | `bool`            | If `True`, raise an error immediately if the row is already locked (`FOR UPDATE NOWAIT`) |
| `skip_locked` | `bool`            | If `True`, skip already-locked rows (`FOR UPDATE SKIP LOCKED`)                           |
| `of`          | `tuple[str, ...]` | Specific tables to lock in a multi-table query                                           |

**SQL generated:**

```sql
SELECT * FROM accounts WHERE id = $1 FOR UPDATE
-- With nowait:
SELECT * FROM accounts WHERE id = $1 FOR UPDATE NOWAIT
-- With skip_locked:
SELECT * FROM accounts WHERE id = $1 FOR UPDATE SKIP LOCKED
```

**Example:**

```python
# Basic row locking
async with db.transaction():
    account = await Account.objects.filter(id=1).select_for_update().get()
    account.balance -= 100
    await account.save()

# NOWAIT — fail immediately if locked
async with db.transaction():
    try:
        account = (
            await Account.objects.filter(id=1).select_for_update(nowait=True).get()
        )
    except DatabaseError:
        # Row is locked by another transaction
        pass

# SKIP LOCKED — process unlocked rows (job queue pattern)
async with db.transaction():
    jobs = (
        await Job.objects.filter(status="pending")
        .select_for_update(skip_locked=True)
        .limit(10)
    )
    for job in jobs:
        job.status = "processing"
        await job.save()
```

**Return type:** `QuerySet`

Must be used inside a `db.transaction()`. The lock is released when the transaction commits or rolls back.

---

### `raw(sql, params=()) -> QuerySet`

Execute raw SQL and return the results mapped to model instances. For queries that do not map to models, use `db.query()` instead.

**Signature:**

```python
Model.objects.raw(sql, params=()) -> QuerySet
```

**Parameters:**

| Parameter | Type    | Description                                   |
| --------- | ------- | --------------------------------------------- |
| `sql`     | `str`   | Raw SQL query with `$1, $2, ...` placeholders |
| `params`  | `tuple` | Parameter values                              |

**SQL generated:** The exact SQL you provide.

**Example:**

```python
# Raw query returning model instances
articles = await Article.objects.raw(
    "SELECT * FROM articles WHERE views > $1 ORDER BY views DESC", (100,)
)

# With complex SQL
articles = await Article.objects.raw(
    """
    SELECT a.*, COUNT(c.id) as comment_count
    FROM articles a
    LEFT JOIN comments c ON c.article_id = a.id
    WHERE a.published = $1
    GROUP BY a.id
    ORDER BY comment_count DESC
""",
    (True,),
)
```

**Return type:** `QuerySet` (model instances with extra attributes from raw columns)

For non-model queries, use [Raw SQL](#raw-sql) (`db.query()`).

---

## QuerySet API — Methods That Evaluate

These methods execute a database query and return results. They are terminal — you cannot chain further QuerySet methods after them.

---

### `get(**kwargs) -> Model`

Returns exactly one object matching the given lookups. Raises `DoesNotExist` if no object is found, or `MultipleObjectsReturned` if more than one object matches.

**Signature:**

```python
await QuerySet.get(**kwargs) -> Model
```

**Parameters:**

| Parameter  | Type              | Description                        |
| ---------- | ----------------- | ---------------------------------- |
| `**kwargs` | keyword arguments | Field lookups (same as `filter()`) |

**SQL generated:**

```sql
SELECT * FROM articles WHERE id = $1 LIMIT 2
```

The `LIMIT 2` is used internally to detect if more than one row matches.

**Example:**

```python
# Get by primary key
article = await Article.objects.get(id=1)

# Get by unique field
user = await User.objects.get(email="alice@example.com")

# Chained with filter
article = await Article.objects.filter(published=True).get(title="Hello World")

# Handle exceptions
from hyperdjango.models import DoesNotExist, MultipleObjectsReturned

try:
    article = await Article.objects.get(id=999)
except DoesNotExist:
    print("Not found")
except MultipleObjectsReturned:
    print("Multiple matches")
```

**Return type:** Model instance

---

### `create(**kwargs) -> Model`

Creates a new object, saves it to the database, and returns it. Equivalent to instantiating, setting fields, and calling `save()`.

**Signature:**

```python
await QuerySet.create(**kwargs) -> Model
```

**Parameters:**

| Parameter  | Type              | Description                  |
| ---------- | ----------------- | ---------------------------- |
| `**kwargs` | keyword arguments | Field names and their values |

**SQL generated:**

```sql
INSERT INTO articles (title, published, views) VALUES ($1, $2, $3) RETURNING *
```

**Example:**

```python
# Create and save in one step
article = await Article.objects.create(
    title="New Article",
    content="Body text",
    published=True,
    author_id=1,
)
print(article.id)  # auto-assigned by PostgreSQL
```

**Return type:** Model instance (with auto-generated fields populated)

---

### `get_or_create(defaults=None, **kwargs) -> tuple[Model, bool]`

Looks up an object by the given kwargs. If not found, creates it using the `defaults` dict. Returns a tuple of `(object, created)`.

**Signature:**

```python
await QuerySet.get_or_create(defaults=None, **kwargs) -> tuple[Model, bool]
```

**Parameters:**

| Parameter  | Type                       | Description                                          |
| ---------- | -------------------------- | ---------------------------------------------------- |
| `defaults` | `dict[str, ...]` or `None` | Fields to set only on creation (not used for lookup) |
| `**kwargs` | keyword arguments          | Fields used for the lookup                           |

**SQL generated:**

```sql
-- First tries:
SELECT * FROM articles WHERE title = $1 LIMIT 2
-- If not found:
INSERT INTO articles (title, content, published) VALUES ($1, $2, $3) RETURNING *
```

**Example:**

```python
# Lookup by title, create with defaults if not found
article, created = await Article.objects.get_or_create(
    title="Welcome",
    defaults={"content": "Hello world", "published": True},
)
if created:
    print("Created new article")
else:
    print("Found existing article")
```

**Return type:** `tuple[Model, bool]` — the model instance and whether it was created

---

### `update_or_create(defaults=None, **kwargs) -> tuple[Model, bool]`

Like `get_or_create()`, but if the object already exists, updates it with the `defaults` dict.

**Signature:**

```python
await QuerySet.update_or_create(defaults=None, **kwargs) -> tuple[Model, bool]
```

**Parameters:**

| Parameter  | Type                       | Description                         |
| ---------- | -------------------------- | ----------------------------------- |
| `defaults` | `dict[str, ...]` or `None` | Fields to set on creation OR update |
| `**kwargs` | keyword arguments          | Fields used for the lookup          |

**SQL generated:**

```sql
-- First tries:
SELECT * FROM articles WHERE title = $1 LIMIT 2
-- If found, updates:
UPDATE articles SET content = $1, published = $2 WHERE id = $3
-- If not found, inserts:
INSERT INTO articles (title, content, published) VALUES ($1, $2, $3) RETURNING *
```

**Example:**

```python
# Upsert pattern: create or update
article, created = await Article.objects.update_or_create(
    title="Daily Report",
    defaults={"content": "Updated content", "published": True},
)
# If "Daily Report" existed, content and published are updated
# If it didn't exist, a new article is created with all fields
```

**Return type:** `tuple[Model, bool]` — the model instance and whether it was created

---

### `bulk_create(objs, batch_size=None, ignore_conflicts=False, update_conflicts=False, update_fields=None, unique_fields=None) -> list[Model]`

Inserts multiple objects in a single SQL statement. Dramatically faster than individual `save()` calls.

**Signature:**

```python
await QuerySet.bulk_create(
    objs,
    batch_size=None,
    ignore_conflicts=False,
    update_conflicts=False,
    update_fields=None,
    unique_fields=None,
) -> list[Model]
```

**Parameters:**

| Parameter          | Type                  | Description                                                                              |
| ------------------ | --------------------- | ---------------------------------------------------------------------------------------- |
| `objs`             | `list[Model]`         | Model instances to insert                                                                |
| `batch_size`       | `int` or `None`       | Maximum number of objects per INSERT statement                                           |
| `ignore_conflicts` | `bool`                | If `True`, silently skip rows that violate unique constraints (`ON CONFLICT DO NOTHING`) |
| `update_conflicts` | `bool`                | If `True`, update existing rows on conflict (`ON CONFLICT ... DO UPDATE`)                |
| `update_fields`    | `list[str]` or `None` | Fields to update on conflict (required when `update_conflicts=True`)                     |
| `unique_fields`    | `list[str]` or `None` | Fields that define the unique constraint for conflict detection                          |

**SQL generated:**

```sql
-- Basic bulk insert:
INSERT INTO articles (title, content, published) VALUES
    ($1, $2, $3),
    ($4, $5, $6),
    ($7, $8, $9)
RETURNING *

-- With ignore_conflicts:
INSERT INTO articles (title, content) VALUES ($1, $2), ($3, $4)
ON CONFLICT DO NOTHING

-- With update_conflicts:
INSERT INTO articles (title, content) VALUES ($1, $2), ($3, $4)
ON CONFLICT (title) DO UPDATE SET content = EXCLUDED.content
```

**Example:**

```python
# Insert 100 articles in one query
articles = [Article(title=f"Article {i}") for i in range(100)]
created = await Article.objects.bulk_create(articles)

# With batch_size (split into smaller INSERTs)
articles = [Article(title=f"Article {i}") for i in range(10000)]
await Article.objects.bulk_create(articles, batch_size=1000)

# Ignore duplicates
await Article.objects.bulk_create(
    [Article(title="Unique Title", content="body")],
    ignore_conflicts=True,
)

# Upsert pattern
await Article.objects.bulk_create(
    [Article(title="Daily Report", content="Updated body")],
    update_conflicts=True,
    update_fields=["content"],
    unique_fields=["title"],
)
```

**Return type:** `list[Model]`

For truly massive inserts (100K+ rows), use the native COPY protocol via `db.copy_in()` which achieves 536K rows/sec.

---

### `bulk_update(objs, fields, batch_size=None) -> int`

Updates specific fields on multiple objects in a single query.

**Signature:**

```python
await QuerySet.bulk_update(objs, fields, batch_size=None) -> int
```

**Parameters:**

| Parameter    | Type            | Description                                    |
| ------------ | --------------- | ---------------------------------------------- |
| `objs`       | `list[Model]`   | Model instances with updated values            |
| `fields`     | `list[str]`     | Field names to update                          |
| `batch_size` | `int` or `None` | Maximum number of objects per UPDATE statement |

**SQL generated:**

```sql
UPDATE articles SET views = CASE
    WHEN id = $1 THEN $2
    WHEN id = $3 THEN $4
END
WHERE id IN ($1, $3)
```

**Example:**

```python
# Fetch, modify, bulk update
articles = await Article.objects.filter(published=True).all()
for article in articles:
    article.views += 1

count = await Article.objects.bulk_update(articles, ["views"])
print(f"Updated {count} articles")

# With batch_size
await Article.objects.bulk_update(articles, ["views", "title"], batch_size=500)
```

**Return type:** `int` (number of rows updated)

The primary key field cannot be updated. All objects must have their primary key set.

---

### `count() -> int`

Returns the number of rows matching the query. More efficient than `len(await qs.all())` because it uses `SELECT COUNT(*)`.

**Signature:**

```python
await QuerySet.count() -> int
```

**Parameters:** None.

**SQL generated:**

```sql
SELECT COUNT(*) FROM articles WHERE published = $1
```

**Example:**

```python
# Total count
total = await Article.objects.count()

# Filtered count
published_count = await Article.objects.filter(published=True).count()
```

**Return type:** `int`

---

### `in_bulk(id_list=None, field_name="id") -> dict[..., Model]`

Returns a dictionary mapping field values to model instances for the given list of IDs.

**Signature:**

```python
await QuerySet.in_bulk(id_list=None, field_name="id") -> dict[..., Model]
```

**Parameters:**

| Parameter    | Type             | Description                                                |
| ------------ | ---------------- | ---------------------------------------------------------- |
| `id_list`    | `list` or `None` | List of values to look up. If `None`, returns all objects. |
| `field_name` | `str`            | Field to use as the dictionary key (must be unique)        |

**SQL generated:**

```sql
SELECT * FROM articles WHERE id IN ($1, $2, $3)
```

**Example:**

```python
# Get multiple articles by ID, as a dict
articles_by_id = await Article.objects.in_bulk([1, 2, 3])
# {1: <Article id=1>, 2: <Article id=2>, 3: <Article id=3>}

article = articles_by_id[2]

# By a different unique field
users_by_email = await User.objects.in_bulk(
    ["alice@example.com", "bob@example.com"],
    field_name="email",
)
```

**Return type:** `dict[key_type, Model]`

---

### `iterator(chunk_size=2000) -> AsyncIterator[Model]`

Returns an async iterator that fetches results in chunks, keeping memory usage constant for large result sets.

**Signature:**

```python
QuerySet.iterator(chunk_size=2000) -> AsyncIterator[Model]
```

**Parameters:**

| Parameter    | Type  | Description                                     |
| ------------ | ----- | ----------------------------------------------- |
| `chunk_size` | `int` | Number of rows to fetch per database round-trip |

**SQL generated:**

```sql
-- Uses server-side cursors internally:
DECLARE _cursor CURSOR FOR SELECT * FROM articles
FETCH 2000 FROM _cursor
```

**Example:**

```python
# Process millions of rows without loading all into memory
async for article in Article.objects.filter(published=True).iterator(chunk_size=500):
    process(article)
```

**Return type:** `AsyncIterator[Model]`

---

### `latest(*fields) -> Model`

Returns the latest object by the given date/time field(s). Raises `DoesNotExist` if the QuerySet is empty.

**Signature:**

```python
await QuerySet.latest(*fields) -> Model
```

**Parameters:**

| Parameter | Type  | Description                                                             |
| --------- | ----- | ----------------------------------------------------------------------- |
| `*fields` | `str` | Field names to order by (descending). If omitted, uses `Meta.ordering`. |

**SQL generated:**

```sql
SELECT * FROM articles ORDER BY pub_date DESC LIMIT 1
```

**Example:**

```python
# Latest by explicit field
latest_article = await Article.objects.latest("pub_date")

# Latest using Meta.ordering (if ordering = ["-pub_date"])
latest_article = await Article.objects.latest()

# Latest within a filtered set
latest_published = await Article.objects.filter(published=True).latest("pub_date")
```

**Return type:** Model instance

---

### `earliest(*fields) -> Model`

Returns the earliest (oldest) object by the given date/time field(s). Raises `DoesNotExist` if the QuerySet is empty.

**Signature:**

```python
await QuerySet.earliest(*fields) -> Model
```

**Parameters:**

| Parameter | Type  | Description                                                            |
| --------- | ----- | ---------------------------------------------------------------------- |
| `*fields` | `str` | Field names to order by (ascending). If omitted, uses `Meta.ordering`. |

**SQL generated:**

```sql
SELECT * FROM articles ORDER BY pub_date ASC LIMIT 1
```

**Example:**

```python
# Earliest by explicit field
first_article = await Article.objects.earliest("pub_date")

# First user ever created
first_user = await User.objects.earliest("id")
```

**Return type:** Model instance

---

### `first() -> Model | None`

Returns the first object of the QuerySet, or `None` if the QuerySet is empty. Does not raise an exception.

**Signature:**

```python
await QuerySet.first() -> Model | None
```

**Parameters:** None.

**SQL generated:**

```sql
SELECT * FROM articles ORDER BY id ASC LIMIT 1
```

**Example:**

```python
# First by primary key
article = await Article.objects.first()
if article:
    print(article.title)

# First by custom ordering
newest = await Article.objects.order_by("-pub_date").first()
```

**Return type:** `Model | None`

---

### `last() -> Model | None`

Returns the last object of the QuerySet, or `None` if the QuerySet is empty. Reverses the ordering and takes the first result.

**Signature:**

```python
await QuerySet.last() -> Model | None
```

**Parameters:** None.

**SQL generated:**

```sql
SELECT * FROM articles ORDER BY id DESC LIMIT 1
```

**Example:**

```python
# Last by primary key
article = await Article.objects.last()

# Last within ordered set
oldest = await Article.objects.order_by("-pub_date").last()
```

**Return type:** `Model | None`

---

### `aggregate(**kwargs) -> dict[str, ...]`

Returns a dictionary of aggregate values computed over the QuerySet. Unlike `annotate()`, this returns a single dict, not a QuerySet.

**Signature:**

```python
await QuerySet.aggregate(**kwargs) -> dict[str, ...]
```

**Parameters:**

| Parameter  | Type                 | Description                                        |
| ---------- | -------------------- | -------------------------------------------------- |
| `**kwargs` | expression arguments | Named aggregations: `alias=AggregateFunc("field")` |

**SQL generated:**

```sql
SELECT COUNT(id) AS total, AVG(views) AS avg_views, MAX(views) AS max_views
FROM articles
```

**Example:**

```python
# The ORM query primitives (F, Q, and the aggregates) are re-exported from
# hyperdjango.models, so a query can import everything from one place:
from hyperdjango.models import Count, Sum, Avg, Max, Min, StdDev, Variance
# (They also live in hyperdjango.expressions, their source module.)

# Single aggregate
result = await Article.objects.aggregate(total=Count("id"))
# {"total": 150}

# Multiple aggregates
stats = await Article.objects.aggregate(
    total=Count("id"),
    avg_views=Avg("views"),
    max_views=Max("views"),
    min_views=Min("views"),
    total_views=Sum("views"),
)
# {"total": 150, "avg_views": 42.5, "max_views": 1000, "min_views": 0, "total_views": 6375}

# Filtered aggregate
stats = await Article.objects.filter(published=True).aggregate(
    count=Count("id"),
    avg_views=Avg("views"),
)
```

**Return type:** `dict[str, int | float | Decimal | None]`

---

### `exists() -> bool`

Returns `True` if the QuerySet contains any results. More efficient than `count() > 0` because it uses `SELECT 1 ... LIMIT 1`.

**Signature:**

```python
await QuerySet.exists() -> bool
```

**Parameters:** None.

**SQL generated:**

```sql
SELECT 1 FROM articles WHERE published = $1 LIMIT 1
```

**Example:**

```python
# Check existence
has_published = await Article.objects.filter(published=True).exists()
if has_published:
    print("There are published articles")

# More efficient than:
# if await Article.objects.filter(published=True).count() > 0:
```

**Return type:** `bool`

---

### `contains(obj) -> bool`

Returns `True` if the QuerySet contains the given object.

**Signature:**

```python
await QuerySet.contains(obj) -> bool
```

**Parameters:**

| Parameter | Type    | Description                   |
| --------- | ------- | ----------------------------- |
| `obj`     | `Model` | A model instance to check for |

**SQL generated:**

```sql
SELECT 1 FROM articles WHERE id = $1 AND published = $2 LIMIT 1
```

**Example:**

```python
article = await Article.objects.get(id=1)
is_published = await Article.objects.filter(published=True).contains(article)
```

**Return type:** `bool`

---

### `update(**kwargs) -> int`

Updates all rows matched by the QuerySet and returns the number of rows affected. Does not call `save()` or trigger signals.

**Signature:**

```python
await QuerySet.update(**kwargs) -> int
```

**Parameters:**

| Parameter  | Type              | Description                                               |
| ---------- | ----------------- | --------------------------------------------------------- |
| `**kwargs` | keyword arguments | Field names and their new values. Supports F expressions. |

**SQL generated:**

```sql
UPDATE articles SET published = $1 WHERE views > $2
```

**Example:**

```python
# Publish all articles with enough views
count = await Article.objects.filter(views__gt=100).update(published=True)
print(f"Published {count} articles")

# Atomic increment with F expression
from hyperdjango.expressions import F

await Article.objects.filter(id=1).update(views=F("views") + 1)

# Bulk update multiple fields
await Article.objects.filter(archived=True).update(published=False, views=0)
```

**Return type:** `int` (number of rows updated)

This is a SQL-level update. It does NOT call model `save()`, does NOT trigger `pre_save`/`post_save` signals, and does NOT run model validation. For those, use individual `save()` calls.

---

### `delete() -> tuple[int, dict[str, int]]`

Deletes all rows matched by the QuerySet and returns the count of deleted objects (including cascaded deletes).

**Signature:**

```python
await QuerySet.delete() -> tuple[int, dict[str, int]]
```

**Parameters:** None.

**SQL generated:**

```sql
DELETE FROM articles WHERE published = $1
```

**Example:**

```python
# Delete unpublished articles
count, details = await Article.objects.filter(published=False).delete()
print(f"Deleted {count} objects")
# details: {"articles": 5, "comments": 23}  (includes cascaded deletes)

# Delete all (be careful!)
count, details = await Article.objects.all().delete()
```

**Return type:** `tuple[int, dict[str, int]]` — total count and per-table breakdown

This triggers `pre_delete` and `post_delete` signals. For bulk deletion without signals, use raw SQL.

---

### `explain(analyze=False, verbose=False, costs=True, buffers=False, format="text") -> str`

Returns the PostgreSQL query execution plan for the QuerySet.

**Signature:**

```python
await QuerySet.explain(
    analyze=False,
    verbose=False,
    costs=True,
    buffers=False,
    format="text",
) -> str
```

**Parameters:**

| Parameter | Type   | Description                                                         |
| --------- | ------ | ------------------------------------------------------------------- |
| `analyze` | `bool` | If `True`, actually executes the query and reports real timing data |
| `verbose` | `bool` | If `True`, show additional detail for each plan node                |
| `costs`   | `bool` | If `True`, show estimated startup and total costs (default)         |
| `buffers` | `bool` | If `True`, show buffer usage information (requires `analyze=True`)  |
| `format`  | `str`  | Output format: `"text"`, `"json"`, `"xml"`, or `"yaml"`             |

**SQL generated:**

```sql
EXPLAIN SELECT * FROM articles WHERE published = $1

-- With analyze:
EXPLAIN (ANALYZE) SELECT * FROM articles WHERE published = $1

-- Full options:
EXPLAIN (ANALYZE, VERBOSE, BUFFERS, FORMAT JSON) SELECT * FROM articles WHERE published = $1
```

**Example:**

```python
# Basic plan (estimated costs, no execution)
plan = await Article.objects.filter(published=True).explain()
print(plan)
# Seq Scan on articles  (cost=0.00..12.50 rows=100 width=256)
#   Filter: (published = true)

# Analyzed plan (actually executes the query)
plan = await Article.objects.filter(published=True).explain(analyze=True)
print(plan)
# Seq Scan on articles  (cost=0.00..12.50 rows=100 width=256) (actual time=0.015..0.032 rows=75 loops=1)
#   Filter: (published = true)
#   Rows Removed by Filter: 25
# Planning Time: 0.052 ms
# Execution Time: 0.048 ms

# JSON format for programmatic use
plan_json = await Article.objects.filter(published=True).explain(format="json")
```

**Return type:** `str`

Use `explain(analyze=True)` to see real execution times. Without `analyze`, PostgreSQL only estimates costs without executing the query.

---

## Limiting

```python
# First N results
await Article.objects.limit(10)

# Offset + limit (pagination)
await Article.objects.offset(20).limit(10)
```

These map to SQL `LIMIT` and `OFFSET`. For proper pagination with page objects, see [Pagination](pagination.md).

---

## Field Lookups

Field lookups are specified as keyword arguments to `filter()`, `exclude()`, and `get()`. The syntax is `field__lookup=value`. If no lookup is specified, `exact` is assumed.

### String Lookups

---

#### `exact`

Exact match. This is the default lookup when no lookup type is specified.

```python
await Article.objects.filter(id__exact=1).all()
# Shorthand (exact is default):
await Article.objects.filter(id=1).all()
```

**SQL:** `WHERE id = $1`

---

#### `iexact`

Case-insensitive exact match.

```python
await User.objects.filter(username__iexact="Alice").all()
```

**SQL:** `WHERE UPPER(username) = UPPER($1)` (or `WHERE username ILIKE $1`)

---

#### `contains`

Case-sensitive containment test.

```python
await Article.objects.filter(title__contains="Django").all()
```

**SQL:** `WHERE title LIKE '%' || $1 || '%'`

---

#### `icontains`

Case-insensitive containment test.

```python
await Article.objects.filter(title__icontains="django").all()
```

**SQL:** `WHERE title ILIKE '%' || $1 || '%'`

---

#### `startswith`

Case-sensitive starts-with.

```python
await Article.objects.filter(title__startswith="How").all()
```

**SQL:** `WHERE title LIKE $1 || '%'`

---

#### `istartswith`

Case-insensitive starts-with.

```python
await Article.objects.filter(title__istartswith="how").all()
```

**SQL:** `WHERE title ILIKE $1 || '%'`

---

#### `endswith`

Case-sensitive ends-with.

```python
await Article.objects.filter(title__endswith="?").all()
```

**SQL:** `WHERE title LIKE '%' || $1`

---

#### `iendswith`

Case-insensitive ends-with.

```python
await Article.objects.filter(title__iendswith="?").all()
```

**SQL:** `WHERE title ILIKE '%' || $1`

---

#### `regex`

Case-sensitive regular expression match (PostgreSQL `~` operator).

```python
await Article.objects.filter(title__regex=r"^\d+").all()
```

**SQL:** `WHERE title ~ $1`

---

#### `iregex`

Case-insensitive regular expression match (PostgreSQL `~*` operator).

```python
await Article.objects.filter(title__iregex=r"^how").all()
```

**SQL:** `WHERE title ~* $1`

---

### Comparison Lookups

---

#### `gt`

Greater than.

```python
await Article.objects.filter(views__gt=100).all()
```

**SQL:** `WHERE views > $1`

---

#### `gte`

Greater than or equal to.

```python
await Article.objects.filter(views__gte=100).all()
```

**SQL:** `WHERE views >= $1`

---

#### `lt`

Less than.

```python
await Article.objects.filter(views__lt=10).all()
```

**SQL:** `WHERE views < $1`

---

#### `lte`

Less than or equal to.

```python
await Article.objects.filter(views__lte=10).all()
```

**SQL:** `WHERE views <= $1`

---

#### `in`

In a given iterable (list, tuple, or subquery).

```python
# List of values
await Article.objects.filter(id__in=[1, 2, 3]).all()

# Subquery
published_author_ids = Article.objects.filter(published=True).values_list(
    "author_id", flat=True
)
await Author.objects.filter(id__in=published_author_ids).all()
```

**SQL:** `WHERE id IN ($1, $2, $3)`

---

#### `range`

Inclusive range (maps to `BETWEEN`).

```python
import datetime

await Article.objects.filter(views__range=(10, 100)).all()

await Article.objects.filter(
    pub_date__range=(datetime.date(2024, 1, 1), datetime.date(2024, 12, 31))
).all()
```

**SQL:** `WHERE views BETWEEN $1 AND $2`

---

#### `isnull`

Is NULL / IS NOT NULL.

```python
# NULL check
await Article.objects.filter(content__isnull=True).all()

# NOT NULL check
await Article.objects.filter(content__isnull=False).all()
```

**SQL:** `WHERE content IS NULL` / `WHERE content IS NOT NULL`

---

### Date/Time Lookups

These lookups extract components from date, datetime, and time fields.

---

#### `date`

Extract the date portion from a datetime field.

```python
import datetime

await Article.objects.filter(pub_date__date=datetime.date(2024, 6, 15)).all()
```

**SQL:** `WHERE pub_date::date = $1`

---

#### `year`

Extract the year.

```python
await Article.objects.filter(pub_date__year=2024).all()
```

**SQL:** `WHERE EXTRACT(YEAR FROM pub_date) = $1`

---

#### `iso_year`

Extract the ISO 8601 year (may differ from calendar year at year boundaries).

```python
await Article.objects.filter(pub_date__iso_year=2024).all()
```

**SQL:** `WHERE EXTRACT(ISOYEAR FROM pub_date) = $1`

---

#### `month`

Extract the month (1-12).

```python
await Article.objects.filter(pub_date__month=6).all()
```

**SQL:** `WHERE EXTRACT(MONTH FROM pub_date) = $1`

---

#### `day`

Extract the day of the month (1-31).

```python
await Article.objects.filter(pub_date__day=15).all()
```

**SQL:** `WHERE EXTRACT(DAY FROM pub_date) = $1`

---

#### `week`

Extract the ISO 8601 week number (1-52 or 53).

```python
await Article.objects.filter(pub_date__week=25).all()
```

**SQL:** `WHERE EXTRACT(WEEK FROM pub_date) = $1`

---

#### `week_day`

Day of the week (1=Sunday through 7=Saturday).

```python
# Monday articles
await Article.objects.filter(pub_date__week_day=2).all()
```

**SQL:** `WHERE EXTRACT(DOW FROM pub_date) + 1 = $1`

---

#### `iso_week_day`

ISO 8601 day of the week (1=Monday through 7=Sunday).

```python
# Monday articles (ISO)
await Article.objects.filter(pub_date__iso_week_day=1).all()
```

**SQL:** `WHERE EXTRACT(ISODOW FROM pub_date) = $1`

---

#### `quarter`

Quarter of the year (1-4).

```python
await Article.objects.filter(pub_date__quarter=2).all()
```

**SQL:** `WHERE EXTRACT(QUARTER FROM pub_date) = $1`

---

#### `time`

Extract the time portion from a datetime field.

```python
import datetime

await Article.objects.filter(pub_date__time=datetime.time(14, 30)).all()
```

**SQL:** `WHERE pub_date::time = $1`

---

#### `hour`

Extract the hour (0-23).

```python
await Article.objects.filter(pub_date__hour=14).all()
```

**SQL:** `WHERE EXTRACT(HOUR FROM pub_date) = $1`

---

#### `minute`

Extract the minute (0-59).

```python
await Article.objects.filter(pub_date__minute=30).all()
```

**SQL:** `WHERE EXTRACT(MINUTE FROM pub_date) = $1`

---

#### `second`

Extract the second (0-59).

```python
await Article.objects.filter(pub_date__second=0).all()
```

**SQL:** `WHERE EXTRACT(SECOND FROM pub_date) = $1`

---

### Lookup Chaining

Lookups can be chained with date/time transforms:

```python
# Articles published on a Monday in June
await Article.objects.filter(
    pub_date__month=6,
    pub_date__week_day=2,
).all()

# Articles from Q1 2024 with views > 100
await Article.objects.filter(
    pub_date__year=2024,
    pub_date__quarter=1,
    views__gt=100,
).all()
```

---

### Lookup Reference Table

| Lookup            | SQL                         | Example                                    |
| ----------------- | --------------------------- | ------------------------------------------ |
| `exact` (default) | `= $1`                      | `filter(id=1)`                             |
| `iexact`          | `ILIKE $1`                  | `filter(name__iexact="alice")`             |
| `contains`        | `LIKE '%..%'`               | `filter(title__contains="Django")`         |
| `icontains`       | `ILIKE '%..%'`              | `filter(title__icontains="django")`        |
| `startswith`      | `LIKE '..%'`                | `filter(title__startswith="How")`          |
| `istartswith`     | `ILIKE '..%'`               | `filter(title__istartswith="how")`         |
| `endswith`        | `LIKE '%..'`                | `filter(title__endswith="?")`              |
| `iendswith`       | `ILIKE '%..'`               | `filter(title__iendswith="?")`             |
| `gt`              | `> $1`                      | `filter(views__gt=100)`                    |
| `gte`             | `>= $1`                     | `filter(views__gte=100)`                   |
| `lt`              | `< $1`                      | `filter(views__lt=10)`                     |
| `lte`             | `<= $1`                     | `filter(views__lte=10)`                    |
| `in`              | `IN ($1, $2, ...)`          | `filter(id__in=[1, 2, 3])`                 |
| `range`           | `BETWEEN $1 AND $2`         | `filter(views__range=(10, 100))`           |
| `isnull`          | `IS NULL` / `IS NOT NULL`   | `filter(content__isnull=True)`             |
| `regex`           | `~ $1`                      | `filter(title__regex=r"^\d+")`             |
| `iregex`          | `~* $1`                     | `filter(title__iregex=r"^how")`            |
| `date`            | `::date = $1`               | `filter(pub_date__date=date(2024, 6, 15))` |
| `year`            | `EXTRACT(YEAR ...) = $1`    | `filter(pub_date__year=2024)`              |
| `iso_year`        | `EXTRACT(ISOYEAR ...) = $1` | `filter(pub_date__iso_year=2024)`          |
| `month`           | `EXTRACT(MONTH ...) = $1`   | `filter(pub_date__month=6)`                |
| `day`             | `EXTRACT(DAY ...) = $1`     | `filter(pub_date__day=15)`                 |
| `week`            | `EXTRACT(WEEK ...) = $1`    | `filter(pub_date__week=25)`                |
| `week_day`        | `EXTRACT(DOW ...) + 1 = $1` | `filter(pub_date__week_day=2)`             |
| `iso_week_day`    | `EXTRACT(ISODOW ...) = $1`  | `filter(pub_date__iso_week_day=1)`         |
| `quarter`         | `EXTRACT(QUARTER ...) = $1` | `filter(pub_date__quarter=2)`              |
| `time`            | `::time = $1`               | `filter(pub_date__time=time(14, 30))`      |
| `hour`            | `EXTRACT(HOUR ...) = $1`    | `filter(pub_date__hour=14)`                |
| `minute`          | `EXTRACT(MINUTE ...) = $1`  | `filter(pub_date__minute=30)`              |
| `second`          | `EXTRACT(SECOND ...) = $1`  | `filter(pub_date__second=0)`               |

---

## Aggregation Functions

All aggregation functions are imported from `hyperdjango.expressions`.

```python
from hyperdjango.expressions import Count, Sum, Avg, Max, Min, StdDev, Variance
```

---

### `Count(field, distinct=False)`

Counts the number of non-NULL values. Use `distinct=True` to count only distinct values.

```python
from hyperdjango.expressions import Count

# Total articles
stats = await Article.objects.aggregate(total=Count("id"))
# {"total": 150}

# Distinct authors
stats = await Article.objects.aggregate(authors=Count("author_id", distinct=True))
# {"authors": 12}

# As annotation
articles = await Author.objects.annotate(
    article_count=Count("articles__id"),
).all()
```

**SQL:** `COUNT(id)` or `COUNT(DISTINCT author_id)`

---

### `Sum(field)`

Computes the sum of all values. Returns `None` if the QuerySet is empty.

```python
from hyperdjango.expressions import Sum

stats = await Article.objects.aggregate(total_views=Sum("views"))
# {"total_views": 6375}

# Filtered sum
stats = await Article.objects.filter(published=True).aggregate(total_views=Sum("views"))

# As annotation
authors = await Author.objects.annotate(
    total_article_views=Sum("articles__views"),
).all()
```

**SQL:** `SUM(views)`

---

### `Avg(field)`

Computes the arithmetic mean. Returns `None` if the QuerySet is empty.

```python
from hyperdjango.expressions import Avg

stats = await Article.objects.aggregate(avg_views=Avg("views"))
# {"avg_views": 42.5}
```

**SQL:** `AVG(views)`

---

### `Max(field)`

Returns the maximum value. Returns `None` if the QuerySet is empty.

```python
from hyperdjango.expressions import Max

stats = await Article.objects.aggregate(max_views=Max("views"))
# {"max_views": 1000}

# Useful for dates
stats = await Article.objects.aggregate(latest=Max("pub_date"))
```

**SQL:** `MAX(views)`

---

### `Min(field)`

Returns the minimum value. Returns `None` if the QuerySet is empty.

```python
from hyperdjango.expressions import Min

stats = await Article.objects.aggregate(min_views=Min("views"))
# {"min_views": 0}

# Earliest date
stats = await Article.objects.aggregate(earliest=Min("pub_date"))
```

**SQL:** `MIN(views)`

---

### `StdDev(field, sample=False)`

Computes the standard deviation. By default uses population standard deviation. Pass `sample=True` for sample standard deviation.

```python
from hyperdjango.expressions import StdDev

# Population standard deviation
stats = await Article.objects.aggregate(stddev=StdDev("views"))

# Sample standard deviation
stats = await Article.objects.aggregate(stddev=StdDev("views", sample=True))
```

**SQL:** `STDDEV_POP(views)` or `STDDEV_SAMP(views)`

---

### `Variance(field, sample=False)`

Computes the variance. By default uses population variance. Pass `sample=True` for sample variance.

```python
from hyperdjango.expressions import Variance

# Population variance
stats = await Article.objects.aggregate(var=Variance("views"))

# Sample variance
stats = await Article.objects.aggregate(var=Variance("views", sample=True))
```

**SQL:** `VAR_POP(views)` or `VAR_SAMP(views)`

---

### Combining Aggregations

Multiple aggregation functions can be used together in a single `aggregate()` call:

```python
from hyperdjango.expressions import Count, Sum, Avg, Max, Min, StdDev, Variance

stats = await Article.objects.filter(published=True).aggregate(
    count=Count("id"),
    total_views=Sum("views"),
    avg_views=Avg("views"),
    max_views=Max("views"),
    min_views=Min("views"),
    stddev_views=StdDev("views"),
    variance_views=Variance("views"),
)
# Single query, one result dict
```

**SQL:**

```sql
SELECT
    COUNT(id) AS count,
    SUM(views) AS total_views,
    AVG(views) AS avg_views,
    MAX(views) AS max_views,
    MIN(views) AS min_views,
    STDDEV_POP(views) AS stddev_views,
    VAR_POP(views) AS variance_views
FROM articles
WHERE published = true
```

---

## Q Objects

Build complex queries with `AND`, `OR`, and `NOT` using `Q` objects.

```python
from hyperdjango.expressions import Q
```

### OR Queries

```python
# Published OR has many views
await Article.objects.filter(Q(published=True) | Q(views__gt=1000)).all()
```

**SQL:** `WHERE published = true OR views > 1000`

### AND Queries

```python
# Published AND recent (Q objects default to AND)
await Article.objects.filter(Q(published=True) & Q(pub_date__year=2024)).all()
```

**SQL:** `WHERE published = true AND EXTRACT(YEAR FROM pub_date) = 2024`

### NOT Queries

```python
# NOT published
await Article.objects.filter(~Q(published=True)).all()
```

**SQL:** `WHERE NOT published = true`

### Complex Combinations

```python
# (published OR featured) AND NOT archived
await Article.objects.filter(
    (Q(published=True) | Q(featured=True)) & ~Q(archived=True)
).all()
```

**SQL:** `WHERE (published = true OR featured = true) AND NOT archived = true`

### Mixing Q Objects with Keyword Arguments

```python
# Keyword args are ANDed, Q objects allow OR
await Article.objects.filter(
    Q(title__contains="Python") | Q(title__contains="Django"),
    published=True,  # ANDed with the Q expression
).all()
```

**SQL:** `WHERE (title LIKE '%Python%' OR title LIKE '%Django%') AND published = true`

### Dynamic Q Construction

```python
# Build queries programmatically
conditions = Q()
for tag_name in ["python", "django", "async"]:
    conditions |= Q(tags__name=tag_name)
await Article.objects.filter(conditions).all()
```

---

## F Expressions

Reference database columns in queries without loading them into Python. Enables atomic operations and column-to-column comparisons.

```python
from hyperdjango.expressions import F
```

### Atomic Updates

```python
# Increment views atomically (no race condition)
await Article.objects.filter(id=1).update(views=F("views") + 1)
```

**SQL:** `UPDATE articles SET views = views + 1 WHERE id = 1`

### Arithmetic Operations

```python
# Decrement
await Account.objects.filter(id=1).update(balance=F("balance") - 100)

# Multiply
await Product.objects.filter(id=1).update(price=F("price") * 1.1)

# Divide
await Stats.objects.update(average=F("total") / F("count"))

# Modulo
await Article.objects.update(bucket=F("id") % 10)
```

### Column Comparisons

```python
# Articles where views exceed the minimum threshold
await Article.objects.filter(views__gt=F("min_views")).all()

# Entries where word count exceeds the summary limit
await Article.objects.filter(word_count__gt=F("summary_limit")).all()
```

**SQL:** `WHERE views > min_views`

### F Expressions with Annotations

```python
from hyperdjango.expressions import F, Value

# Computed column
articles = await Article.objects.annotate(
    double_views=F("views") * 2,
).all()

# Difference between fields
articles = (
    await Article.objects.annotate(
        view_deficit=F("target_views") - F("views"),
    )
    .filter(view_deficit__gt=0)
    .all()
)
```

### F Expressions in order_by

```python
# Order by computed value
await Article.objects.order_by(F("views").desc(nulls_last=True)).all()
```

---

## Raw SQL

For queries that cannot be expressed with the ORM, use the `db` interface directly. All raw queries go through pg.zig.

### `db.query(sql, *params) -> list[tuple]`

Execute a SELECT and return rows as tuples.

```python
rows = await db.query("SELECT * FROM articles WHERE views > $1", 100)
for row in rows:
    print(row)
```

### `db.query_val(sql, *params) -> Any`

Execute a query and return a single scalar value.

```python
count = await db.query_val("SELECT COUNT(*) FROM articles")
max_views = await db.query_val(
    "SELECT MAX(views) FROM articles WHERE published = $1", True
)
```

### `db.execute(sql, *params) -> int`

Execute an INSERT, UPDATE, or DELETE and return the number of affected rows.

```python
affected = await db.execute(
    "UPDATE articles SET views = views + 1 WHERE id = $1", article_id
)
```

### `db.copy_in(table, columns, rows)`

Bulk-insert using PostgreSQL's COPY protocol (536K rows/sec).

```python
rows = [(f"Article {i}", f"Content {i}", True) for i in range(100000)]
await db.copy_in("articles", ["title", "content", "published"], rows)
```

---

## Transactions

### Basic Transactions

```python
async with db.transaction():
    article = await Article.objects.create(title="Draft")
    await Choice.objects.create(question_id=article.id, choice_text="Yes")
    # Both committed together, or both rolled back on error
```

### Nested Transactions (SAVEPOINTs)

```python
async with db.transaction():
    await do_outer_work()
    async with db.transaction():  # SAVEPOINT
        await do_inner_work()  # can fail independently

# Triple nesting
async with db.transaction():
    await step_one()
    async with db.transaction():  # SAVEPOINT sp1
        await step_two()
        async with db.transaction():  # SAVEPOINT sp2
            await step_three()  # can fail without rolling back sp1
```

### Transaction with select_for_update

```python
async with db.transaction():
    # Lock the account row
    account = await Account.objects.filter(id=1).select_for_update().get()
    if account.balance >= amount:
        account.balance -= amount
        await account.save()
    else:
        raise InsufficientFunds()
    # Lock released on commit/rollback
```

---

## Model Inheritance

### Abstract Models

Share fields without creating a table:

```python
class TimestampMixin(Model):
    class Meta:
        abstract = True

    created_at: datetime | None = Field(default=None)
    updated_at: datetime | None = Field(default=None)


class Article(TimestampMixin, Model):
    class Meta:
        table = "articles"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field()
    # Inherits created_at and updated_at
```

Abstract models never create a database table. They exist purely to share field definitions and methods across concrete models.

### Proxy Models

Same table, different Python class:

```python
class PublishedArticle(Article):
    class Meta:
        proxy = True

    # Same table, can add methods or override behavior

    @classmethod
    async def get_recent(cls, days=7):
        cutoff = datetime.now() - timedelta(days=days)
        return await cls.objects.filter(published=True, pub_date__gte=cutoff).all()
```

Proxy models share the same database table as their parent. They can add Python methods, change default ordering, or customize the manager, but cannot add new fields.

### Single-Table Inheritance (STI)

Multiple model types sharing one table, distinguished by a discriminator column:

```python
class Vehicle(Model):
    class Meta:
        table = "vehicles"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field()
    type: str = Field(default="vehicle")  # discriminator column
    top_speed: int = Field(default=0)


class Car(Vehicle):
    class Meta:
        sti = True  # shares parent's table
        sti_type = "car"  # discriminator value

    doors: int = Field(default=4)


class Truck(Vehicle):
    class Meta:
        sti = True
        sti_type = "truck"

    payload_tons: int = Field(default=0)
```

**How it works:**

- All rows live in `vehicles` table. `Car` and `Truck` add nullable columns.
- `Car.objects.all()` auto-adds `WHERE type = 'car'` — you only see cars.
- `Vehicle.objects.all()` returns all rows (cars, trucks, base vehicles).
- `car.save()` auto-sets `type = 'car'` before INSERT.

**Configuration options:**

| Meta attribute | Default             | Description                        |
| -------------- | ------------------- | ---------------------------------- |
| `sti`          | `False`             | Enable single-table inheritance    |
| `sti_type`     | `classname.lower()` | Discriminator value for this child |
| `sti_column`   | `"type"`            | Name of the discriminator column   |

**When to use STI vs separate tables:**

- **STI**: Few child types, similar fields, query across types often. One table, one index.
- **Separate tables**: Many fields per child, rarely query across types, strict nullability.

### Multi-Level Inheritance

```python
class BaseContent(Model):
    class Meta:
        abstract = True

    id: int = Field(primary_key=True, auto=True)
    title: str = Field()
    created_at: datetime | None = Field(default=None)


class Article(BaseContent):
    class Meta:
        table = "articles"

    content: str = Field(default="")
    published: bool = Field(default=False)


class Video(BaseContent):
    class Meta:
        table = "videos"

    url: str = Field()
    duration: int = Field(default=0)
```

---

## Model Mixins

Built-in mixins for common patterns:

```python
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
```

### TimestampMixin

Auto-managed `created_at` and `updated_at` fields. `created_at` is set on first save, `updated_at` is set on every save.

### SoftDeleteMixin

Adds `deleted_at` field. Calling `.delete()` sets the timestamp instead of removing the row. Queries automatically exclude soft-deleted records. Use `.hard_delete()` to permanently remove.

```python
await article.delete()  # Sets deleted_at, row still in DB
await article.hard_delete()  # Actually removes the row

# Include soft-deleted in queries
all_including_deleted = await Article.objects.with_deleted().all()

# Restore a soft-deleted object
await article.restore()
```

### OwnershipMixin

Tracks `created_by` and `updated_by` user references.

```python
await article.save_as(user)  # Sets created_by/updated_by automatically
```

### VersionedMixin

Append-only versioning. Every save creates a new version record.

```python
history = await article.get_history()
# [<ArticleVersion v3>, <ArticleVersion v2>, <ArticleVersion v1>]
```

### Composing Mixins

```python
class AuditedArticle(
    TimestampMixin, SoftDeleteMixin, OwnershipMixin, VersionedMixin, Model
):
    class Meta:
        table = "audited_articles"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field()
    # Has: created_at, updated_at, deleted_at, created_by, updated_by, version history
```

---

## Model Instance Methods

```python
article = Article(title="Hello")

# Save (INSERT on first save, UPDATE after)
await article.save()

# Check if persisted
article.is_persisted  # True after save

# Primary key
article.pk  # shortcut for article.id

# Refresh from database
await article.refresh_from_db()

# Delete
await article.delete()
```

### `save()`

Inserts a new row (if the object has no primary key or has not been saved before) or updates an existing row.

```python
article = Article(title="Draft", published=False)
await article.save()  # INSERT
print(article.id)  # auto-assigned

article.published = True
await article.save()  # UPDATE
```

**SQL:**

```sql
-- First save:
INSERT INTO articles (title, published) VALUES ($1, $2) RETURNING id
-- Subsequent saves:
UPDATE articles SET title = $1, published = $2 WHERE id = $3
```

### `refresh_from_db()`

Reloads the object's field values from the database.

```python
article = await Article.objects.get(id=1)
# ... another process updates the article ...
await article.refresh_from_db()
# article now has the latest values
```

### `delete()`

Deletes the object from the database.

```python
article = await Article.objects.get(id=1)
await article.delete()
# article.pk is now None
```

---

## Composite Primary Keys

```python
class OrderItem(Model):
    class Meta:
        table = "order_items"

    order_id: int = Field(primary_key=True)
    product_id: int = Field(primary_key=True)
    quantity: int = Field(default=1)


# Composite PK as tuple
item = await OrderItem.objects.get(order_id=1, product_id=42)
item.pk  # (1, 42)
```

Composite primary keys are fully supported. The `.pk` property returns a tuple of the key fields.

---

## Meta Options

Control model behavior with `Meta` inner class options:

```python
class Article(Model):
    class Meta:
        table = "articles"
        ordering = ["-pub_date"]  # Default sort order
        abstract = False  # Set True for abstract base models
        proxy = False  # Set True for proxy models
        unlogged = False  # Set True for UNLOGGED tables (no WAL, fast, lost on crash)
        append_only = False  # Set True to block UPDATE/DELETE at the DB tier
        cache_ttl = 60  # Query cache TTL in seconds
        database = "default"  # Database alias for multi-db routing
```

| Option        | Type        | Description                                                                                                                                  |
| ------------- | ----------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| `table`       | `str`       | PostgreSQL table name (required for concrete models)                                                                                         |
| `ordering`    | `list[str]` | Default `ORDER BY` columns. Prefix `-` for descending.                                                                                       |
| `abstract`    | `bool`      | `True` for abstract base models (no table created)                                                                                           |
| `proxy`       | `bool`      | `True` for proxy models (same table, different class)                                                                                        |
| `unlogged`    | `bool`      | `True` for UNLOGGED tables — no WAL writes, faster inserts, data lost on crash. Ideal for caches, rate-limit logs, analytics scratch tables. |
| `append_only` | `bool`      | `True` makes the table append-only at the database tier — see below.                                                                         |
| `cache_ttl`   | `int`       | Query cache TTL in seconds (see [Query Cache](query-cache.md))                                                                               |
| `database`    | `str`       | Target database for multi-db routing (see [Multi-Database](multi-db.md))                                                                     |

### `Meta.append_only` — database-enforced immutability

`append_only = True` makes rows immutable **at the database tier**, not by
application convention. `hyper setup` emits a `BEFORE UPDATE OR DELETE` trigger
that raises an exception, so any `UPDATE` or `DELETE` — from the ORM, a stray
`.update()`/`.delete()`, or a hand SQL session — is rejected. `INSERT` and
`SELECT` are unaffected: the table stays writable for appends only.

```python
class AuditLog(Model):
    class Meta:
        table = "audit_log"
        append_only = True  # forensic integrity by construction

    id: int = Field(primary_key=True, auto=True, big=True)
    actor_id: int = Field(big=True, index=True)
    action: str = Field()
    payload: dict = Field(default_factory=dict)  # JSONB
    created_at: datetime = Field(db_default=DatabaseDefault("now()"))
```

Any attempt to mutate an existing row raises a database error:

```python
log = await AuditLog.objects.create(actor_id=1, action="login")
log.action = "logout"
await log.save()  # raises: append-only table audit_log: UPDATE is not permitted
```

Ideal for audit, consent, and outcome ledgers where history must never be
rewritten. The DDL is idempotent — the shared guard function is
`CREATE OR REPLACE` and the per-table trigger is dropped-then-created, so
re-running `hyper setup` is a no-op.

---

## Database Functions

Use native PostgreSQL functions via raw SQL:

```python
# COALESCE — return first non-null value
await db.query("SELECT COALESCE(bio, 'No bio') FROM users WHERE id = $1", 1)

# EXTRACT — pull date/time components
await db.query("SELECT EXTRACT(YEAR FROM created_at) as year FROM articles")

# CONCAT — string concatenation
await db.query("SELECT CONCAT(first_name, ' ', last_name) as full_name FROM users")

# CAST — type conversion
await db.query("SELECT CAST(views AS TEXT) FROM articles WHERE id = $1", 1)

# NOW — current timestamp
await db.query("SELECT * FROM articles WHERE pub_date <= NOW()")

# LOWER / UPPER
await db.query("SELECT * FROM users WHERE LOWER(username) = $1", "alice")

# LENGTH
await db.query("SELECT * FROM articles WHERE LENGTH(title) > $1", 100)

# SUBSTRING
await db.query("SELECT SUBSTRING(title FROM 1 FOR 50) as short_title FROM articles")

# ARRAY_AGG
await db.query("""
    SELECT author_id, ARRAY_AGG(title) as titles
    FROM articles
    GROUP BY author_id
""")

# JSON operators
await db.query(
    "SELECT meta->>'key' FROM articles WHERE meta @> $1::jsonb", '{"type": "tutorial"}'
)

# Window functions
await db.query("""
    SELECT title, views,
        ROW_NUMBER() OVER (ORDER BY views DESC) as rank
    FROM articles
    WHERE published = true
""")

# Common Table Expressions (CTE)
await db.query(
    """
    WITH popular AS (
        SELECT * FROM articles WHERE views > $1
    )
    SELECT a.*, u.name as author_name
    FROM popular a
    JOIN users u ON u.id = a.author_id
""",
    1000,
)
```

---

## Query Cache

Automatic caching with version-based invalidation:

```python
class Article(Model):
    class Meta:
        table = "articles"
        cache_ttl = 60  # Cache queries for 60 seconds

    id: int = Field(primary_key=True, auto=True)
    title: str = Field()
```

When `cache_ttl` is set, queries are cached automatically. The cache is invalidated when any write operation (`create`, `update`, `delete`, `bulk_create`, `bulk_update`) occurs on the model. FK dependencies are tracked so that a write to a related model also invalidates the cache.

See [Query Cache](query-cache.md) for full documentation including cache warming, statistics, and manual invalidation.

---

## Model Signals

Hook into model lifecycle events with signals:

```python
from hyperdjango.signals import pre_save, post_save, pre_delete, post_delete


@post_save.connect(sender=Article)
async def on_article_saved(sender, instance, created, **kwargs):
    if created:
        print(f"New article: {instance.title}")


@pre_delete.connect(sender=Article)
async def on_article_deleting(sender, instance, **kwargs):
    print(f"About to delete: {instance.title}")


@post_delete.connect(sender=Article)
async def on_article_deleted(sender, instance, **kwargs):
    print(f"Deleted article: {instance.title}")


@pre_save.connect(sender=Article)
async def on_article_saving(sender, instance, **kwargs):
    # Modify before save
    instance.updated_at = datetime.now()
```

Available signals:

| Signal        | When it fires                               |
| ------------- | ------------------------------------------- |
| `pre_save`    | Before `save()` (both INSERT and UPDATE)    |
| `post_save`   | After `save()` (includes `created` boolean) |
| `pre_delete`  | Before `delete()`                           |
| `post_delete` | After `delete()`                            |

See [Signals](signals.md) for the full signal API including custom signals, `pre_init`, `post_init`, and user authentication signals.

---

## Performance

The pg.zig native driver delivers:

- **SELECT by PK**: 2x faster than psycopg3
- **SELECT range**: 4x faster
- **COPY bulk import**: 43x faster (536K rows/sec)
- **Prepared statement caching**: repeated queries skip Parse phase
- **Connection pipelining**: 20 queries in 0.24ms vs 1.40ms sequential

### Performance Tips

1. **Use `select_related()`** for forward FK lookups to avoid N+1 queries.
2. **Use `prefetch_related()`** for reverse FK and M2M lookups.
3. **Use `values()` or `values_list()`** when you only need specific columns.
4. **Use `defer()`** to skip loading large text/binary columns.
5. **Use `only()`** when you need just a few fields from a wide table.
6. **Use `exists()`** instead of `count() > 0` for existence checks.
7. **Use `bulk_create()`** for batch inserts instead of individual `save()` calls.
8. **Use `bulk_update()`** for batch updates instead of individual `save()` calls.
9. **Use `update()`** for SQL-level updates that do not need model validation or signals.
10. **Use `iterator()`** for processing large result sets without loading everything into memory.
11. **Use `select_for_update(skip_locked=True)`** for job queue patterns.
12. **Use `explain(analyze=True)`** to diagnose slow queries.
13. **Use `db.copy_in()`** for truly massive inserts (100K+ rows).
14. **Set `Meta.cache_ttl`** on read-heavy models for automatic query caching.
15. **Use `F()` expressions** for atomic counter updates to avoid race conditions.
