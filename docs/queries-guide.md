# Database Queries Guide

Comprehensive guide to HyperDjango's QuerySet API, lookups, aggregation, expressions, raw SQL, and transactions.

All database operations are async and flow through the native pg.zig PostgreSQL driver with prepared statement caching and connection pooling.

---

## Table of Contents

- [QuerySet Basics](#queryset-basics)
- [Filtering](#filtering)
- [Field Lookups](#field-lookups)
- [Ordering, Limiting, and Pagination](#ordering-limiting-and-pagination)
- [Values and Column Selection](#values-and-column-selection)
- [Aggregation](#aggregation)
- [F Expressions](#f-expressions)
- [Q Objects and Complex Queries](#q-objects-and-complex-queries)
- [Relations: select_related and prefetch_related](#relations-select_related-and-prefetch_related)
- [Annotations](#annotations)
- [Creating, Updating, and Deleting](#creating-updating-and-deleting)
- [Raw SQL](#raw-sql)
- [Transactions](#transactions)
- [Multi-Database Routing](#multi-database-routing)
- [Query Caching](#query-caching)
- [Migration Notes for Django Users](#migration-notes-for-django-users)

---

## QuerySet Basics

Every model has a `.objects` attribute that returns a `QuerySet`. QuerySets are chainable and immutable -- each method returns a new QuerySet.

```python
from models import Order, Product

# Chain methods -- each returns a NEW queryset
qs = Product.objects.filter(is_active=True).order_by("-created_at").limit(20)

# Terminal methods execute the query (all are async)
products = await qs.all()  # List of model instances
product = await qs.first()  # First result or None
product = await qs.get(id=1)  # Exactly one result (raises on 0 or 2+)
count = await qs.count()  # SELECT COUNT(*)
exists = await qs.exists()  # SELECT EXISTS(...)
```

### Terminal Methods

| Method                     | Returns        | Description                                                          |
| -------------------------- | -------------- | -------------------------------------------------------------------- |
| `await qs.all()`           | `list[Model]`  | All matching rows                                                    |
| `await qs.get(**kw)`       | `Model`        | Exactly one row (raises `DoesNotExist` or `MultipleObjectsReturned`) |
| `await qs.first()`         | `Model / None` | First row or None                                                    |
| `await qs.last()`          | `Model / None` | Last row or None                                                     |
| `await qs.latest(field)`   | `Model`        | Most recent by field                                                 |
| `await qs.earliest(field)` | `Model`        | Oldest by field                                                      |
| `await qs.count()`         | `int`          | Count of matching rows                                               |
| `await qs.exists()`        | `bool`         | True if any rows match                                               |
| `await qs.aggregate(**kw)` | `dict`         | Aggregate values                                                     |
| `await qs.explain()`       | `str`          | PostgreSQL EXPLAIN output                                            |

---

## Filtering

### Basic Filtering

```python
# Exact match (default lookup)
active_products = await Product.objects.filter(is_active=True).all()

# Multiple conditions (AND)
cheap_widgets = await Product.objects.filter(
    category="widgets",
    price__lt=50.0,
    stock__gt=0,
).all()

# Exclude
non_draft_orders = await Order.objects.exclude(status="draft").all()
```

### Chained Filters

Multiple `.filter()` calls are combined with AND:

```python
qs = Product.objects
qs = qs.filter(is_active=True)
qs = qs.filter(price__gte=10.0)
qs = qs.filter(price__lte=100.0)
qs = qs.exclude(stock=0)
products = await qs.all()
# WHERE is_active = true AND price >= 10.0 AND price <= 100.0 AND NOT (stock = 0)
```

---

## Field Lookups

Lookups are specified as `field__lookup=value` in filter/exclude calls.

### Comparison Lookups

```python
# Exact (default when no lookup specified)
await Product.objects.filter(status="active").all()
await Product.objects.filter(status__exact="active").all()  # Same as above

# Greater than / less than
expensive = await Product.objects.filter(price__gt=100.0).all()
cheap = await Product.objects.filter(price__lt=10.0).all()
in_range = await Product.objects.filter(price__gte=10.0, price__lte=50.0).all()
```

### String Lookups

```python
# Contains (LIKE '%value%')
results = await Product.objects.filter(name__contains="widget").all()

# Case-insensitive contains (ILIKE '%value%')
results = await Product.objects.filter(name__icontains="Widget").all()

# Starts with / ends with
results = await Product.objects.filter(name__startswith="Pro").all()
results = await Product.objects.filter(name__istartswith="pro").all()
results = await Product.objects.filter(sku__endswith="-XL").all()
results = await Product.objects.filter(sku__iendswith="-xl").all()
```

### Collection Lookups

```python
# IN query
featured = await Product.objects.filter(id__in=[1, 5, 12, 23]).all()

# Range (BETWEEN)
mid_priced = await Product.objects.filter(price__range=(20.0, 80.0)).all()
```

### Null and Boolean Lookups

```python
# IS NULL / IS NOT NULL
no_description = await Product.objects.filter(description__isnull=True).all()
has_description = await Product.objects.filter(description__isnull=False).all()
```

### Date and Time Lookups

```python
from datetime import date, datetime, UTC

# Exact date comparisons
today_orders = await Order.objects.filter(
    created_at__gte=datetime(2025, 1, 1, tzinfo=UTC),
    created_at__lt=datetime(2025, 1, 2, tzinfo=UTC),
).all()

# Year/month/day extraction (transforms)
jan_orders = await Order.objects.filter(created_at__month=1).all()
recent = await Order.objects.filter(created_at__year=2025).all()
```

### Complete Lookup Reference

| Lookup        | SQL                          | Example                           |
| ------------- | ---------------------------- | --------------------------------- |
| `exact`       | `= $1`                       | `filter(name="Widget")`           |
| `iexact`      | `ILIKE $1`                   | `filter(name__iexact="widget")`   |
| `gt`          | `> $1`                       | `filter(price__gt=50)`            |
| `gte`         | `>= $1`                      | `filter(price__gte=50)`           |
| `lt`          | `< $1`                       | `filter(price__lt=50)`            |
| `lte`         | `<= $1`                      | `filter(price__lte=50)`           |
| `contains`    | `LIKE '%' \|\| $1 \|\| '%'`  | `filter(name__contains="wid")`    |
| `icontains`   | `ILIKE '%' \|\| $1 \|\| '%'` | `filter(name__icontains="wid")`   |
| `startswith`  | `LIKE $1 \|\| '%'`           | `filter(name__startswith="Pro")`  |
| `istartswith` | `ILIKE $1 \|\| '%'`          | `filter(name__istartswith="pro")` |
| `endswith`    | `LIKE '%' \|\| $1`           | `filter(sku__endswith="-XL")`     |
| `iendswith`   | `ILIKE '%' \|\| $1`          | `filter(sku__iendswith="-xl")`    |
| `in`          | `= ANY($1)`                  | `filter(id__in=[1, 2, 3])`        |
| `range`       | `BETWEEN $1 AND $2`          | `filter(price__range=(10, 50))`   |
| `isnull`      | `IS NULL` / `IS NOT NULL`    | `filter(deleted_at__isnull=True)` |

### Custom Lookups

Register custom lookups for domain-specific filtering:

```python
from hyperdjango.lookups import register_lookup


# Register a full-text search lookup
@register_lookup("search")
def search_lookup(column, value, param_offset):
    return (
        f"to_tsvector('english', {column}) @@ plainto_tsquery('english', ${param_offset})",
        [value],
    )


# Usage
results = await Product.objects.filter(description__search="ergonomic keyboard").all()
```

---

## Ordering, Limiting, and Pagination

```python
# Order by field (ascending)
products = await Product.objects.order_by("name").all()

# Descending (prefix with -)
products = await Product.objects.order_by("-price").all()

# Multiple ordering fields
products = await Product.objects.order_by("-is_featured", "price").all()

# Limit and offset
page_2 = await Product.objects.order_by("id").limit(25).offset(25).all()

# Distinct
categories = await Product.objects.values("category").distinct().all()
```

### Paginator

For structured pagination with page metadata:

```python
from hyperdjango.paginator import Paginator

paginator = Paginator(Product.objects.filter(is_active=True), per_page=25)
page = await paginator.page(3)

print(page.items)  # List of products on page 3
print(page.number)  # 3
print(page.num_pages)  # Total number of pages
print(page.count)  # Total number of items
print(page.has_next)  # True/False
print(page.has_previous)  # True/False
```

---

## Values and Column Selection

### values() -- Return Dicts

```python
# All fields as dicts
products = await Product.objects.values().all()
# [{"id": 1, "name": "Widget", "price": 29.99, ...}, ...]

# Specific fields only
names_and_prices = await Product.objects.values("name", "price").all()
# [{"name": "Widget", "price": 29.99}, ...]
```

### values_list() -- Return Tuples

```python
prices = await Product.objects.values_list("price").all()
# [(29.99,), (49.99,), ...]

# Flat mode (single field)
names = await Product.objects.values_list("name", flat=True).all()
# ["Widget", "Gadget", ...]
```

### only() and defer() -- Column Selection

```python
# Load only specific columns (SELECT id, name FROM products)
products = await Product.objects.only("id", "name").all()

# Load all EXCEPT specified columns (skip large text fields)
products = await Product.objects.defer("description", "specs_json").all()
```

---

## Aggregation

```python
from hyperdjango.expressions import Avg, Count, Max, Min, Sum

# Single aggregate
total = await Product.objects.aggregate(total_value=Sum("price"))
# {"total_value": 12345.67}

# Multiple aggregates
stats = await Product.objects.aggregate(
    count=Count("id"),
    avg_price=Avg("price"),
    max_price=Max("price"),
    min_price=Min("price"),
    total_stock=Sum("stock"),
)
# {"count": 150, "avg_price": 45.50, "max_price": 299.99, ...}

# Filtered aggregation
active_stats = await Product.objects.filter(is_active=True).aggregate(
    count=Count("id"),
    avg_price=Avg("price"),
)
```

### Group By with values() + annotate()

```python
# Revenue by category
category_revenue = await (
    Order.objects.values("category")
    .annotate(
        total_revenue=Sum("total"),
        order_count=Count("id"),
        avg_order=Avg("total"),
    )
    .order_by("-total_revenue")
    .all()
)
# [
#   {"category": "electronics", "total_revenue": 50000, "order_count": 200, ...},
#   {"category": "clothing", "total_revenue": 30000, ...},
# ]
```

---

## F Expressions

F expressions reference column values in the database, enabling column-to-column comparisons and atomic updates without race conditions.

```python
from hyperdjango.expressions import F

# Filter: products where stock is below reorder level
low_stock = await Product.objects.filter(stock__lt=F("reorder_level")).all()

# Update: increment price by 10% atomically
await Product.objects.filter(category="premium").update(price=F("price") * 1.10)

# Update: decrement stock
await Product.objects.filter(id=product_id).update(stock=F("stock") - 1)

# Annotate: compute profit margin
products = await Product.objects.annotate(
    margin=F("price") - F("cost"),
    margin_pct=(F("price") - F("cost")) / F("price") * 100,
).all()
```

### Combined Expressions

```python
from hyperdjango.expressions import F, Value

# Column arithmetic
await Order.objects.annotate(
    subtotal=F("quantity") * F("unit_price"),
    total_with_tax=F("quantity") * F("unit_price") * Value(1.08),
).all()
```

---

## Q Objects and Complex Queries

For OR conditions and complex boolean logic, use `where_raw()`:

```python
# OR condition: title or body contains search term
results = await (
    Post.objects.where_raw("(title ILIKE {idx} OR body ILIKE {idx})", f"%{search}%")
    .order_by("-created_at")
    .all()
)

# Complex boolean: (status = 'active' AND price < 50) OR (featured = true)
results = await Product.objects.where_raw(
    "((status = {idx} AND price < {idx}) OR featured = {idx})",
    "active",
    50.0,
    True,
).all()
```

The `{idx}` placeholder is replaced with the next available `$N` parameter index. All values are parameterized -- never interpolated into SQL.

---

## Relations: select_related and prefetch_related

### select_related (JOIN)

For forward FK relations. Executes a single query with LEFT JOIN.

```python
# Load orders with their customer and product in ONE query
orders = await (
    Order.objects.select_related("customer", "product")
    .filter(status="pending")
    .order_by("-created_at")
    .limit(50)
    .all()
)

for order in orders:
    # No additional queries -- data was JOINed
    print(f"Order #{order.id}: {order.customer.name} bought {order.product.name}")
```

### Nested select_related

```python
# Book -> Author -> Publisher (two JOINs in one query)
books = await Book.objects.select_related("author__publisher").all()

for book in books:
    print(f"{book.title} by {book.author.name} ({book.author.publisher.name})")
```

### prefetch_related (Separate Queries)

For reverse FK and M2M relations. Executes one additional query per relation (not N+1).

```python
# Load authors with all their books (1 query for authors + 1 query for books)
authors = await Author.objects.prefetch_related("books").all()

for author in authors:
    print(f"{author.name}: {len(author.books)} books")
    for book in author.books:
        print(f"  - {book.title}")
```

### When to Use Which

| Method             | Relation Type   | Queries   | Best For                |
| ------------------ | --------------- | --------- | ----------------------- |
| `select_related`   | Forward FK      | 1 (JOIN)  | Single-valued relations |
| `prefetch_related` | Reverse FK, M2M | N+1 batch | Multi-valued relations  |

---

## Annotations

Add computed columns to query results:

```python
from hyperdjango.expressions import Avg, Count, Max, Sum

# Count related objects
authors = await (
    Author.objects.annotate(book_count=Count("id")).order_by("-book_count").all()
)

for author in authors:
    print(f"{author.name}: {author.book_count} books")

# Computed column with F expressions
products = await (
    Product.objects.annotate(
        revenue=F("price") * F("units_sold"),
        profit=F("price") * F("units_sold") - F("cost") * F("units_sold"),
    )
    .order_by("-revenue")
    .limit(10)
    .all()
)
```

---

## Creating, Updating, and Deleting

### Create

```python
# Single create
product = await Product.objects.create(
    name="Wireless Keyboard",
    sku="KB-WIRELESS-001",
    price=79.99,
    stock=150,
    category="peripherals",
)
print(product.id)  # Auto-assigned by PostgreSQL

# get_or_create
product, created = await Product.objects.get_or_create(
    sku="KB-WIRELESS-001",
    defaults={"name": "Wireless Keyboard", "price": 79.99},
)

# update_or_create
product, created = await Product.objects.update_or_create(
    sku="KB-WIRELESS-001",
    defaults={"price": 69.99, "stock": 200},
)
```

### Update

```python
# Bulk update (returns count of updated rows)
count = await Product.objects.filter(category="clearance").update(
    price=F("price") * 0.5,
    is_active=False,
)
print(f"Marked {count} products as clearance")

# Instance update
product = await Product.objects.get(id=1)
product.price = 89.99
await product.save()
```

### Delete

```python
# Bulk delete (returns count of deleted rows)
count = await Product.objects.filter(is_active=False, stock=0).delete()
print(f"Deleted {count} discontinued products")

# Instance delete
product = await Product.objects.get(id=1)
await product.delete()
```

### bulk_update

```python
products = await Product.objects.filter(category="electronics").all()
for p in products:
    p.price = round(p.price * 1.05, 2)

await Product.objects.bulk_update(products, fields=["price"])
```

---

## Raw SQL

For queries that cannot be expressed through the ORM:

```python
from hyperdjango.database import Database

db = Database("postgres://localhost/mydb")
await db.connect()

# Raw query (returns list of tuples)
rows = await db.query(
    "SELECT id, name, price FROM products WHERE price > $1 ORDER BY price DESC LIMIT $2",
    50.0,
    10,
)

# Raw query returning dicts
rows = await db.query_dicts(
    "SELECT id, name, price FROM products WHERE category = $1",
    "electronics",
)

# Raw execute (returns affected row count)
count = await db.execute(
    "UPDATE products SET price = price * $1 WHERE category = $2",
    0.9,
    "clearance",
)

# COPY for high-speed bulk import (536K rows/sec)
data = [(name, price, stock) for name, price, stock in product_data]
await db.copy_from("products", ["name", "price", "stock"], data)
```

### Pipeline (Batch Queries)

Execute multiple queries in a single network round-trip:

```python
queries = [
    ("SELECT COUNT(*) FROM products WHERE is_active = $1", [True]),
    ("SELECT AVG(price) FROM products WHERE category = $1", ["electronics"]),
    ("SELECT MAX(created_at) FROM orders", []),
]
results = await db.pipeline(queries)
# results[0] = [(150,)]
# results[1] = [(89.99,)]
# results[2] = [(datetime(...),)]
```

Pipeline is 5.7x faster than sequential queries for batched reads.

---

## Transactions

### atomic() Context Manager

```python
from hyperdjango.database import Database

db = Database("postgres://localhost/mydb")

async with db.atomic() as conn:
    # All queries use the same connection with BEGIN/COMMIT
    await conn.execute(
        "UPDATE accounts SET balance = balance - $1 WHERE id = $2",
        100.0,
        sender_id,
    )
    await conn.execute(
        "UPDATE accounts SET balance = balance + $1 WHERE id = $2",
        100.0,
        receiver_id,
    )
    await conn.execute(
        "INSERT INTO transfers (sender_id, receiver_id, amount) VALUES ($1, $2, $3)",
        sender_id,
        receiver_id,
        100.0,
    )
    # COMMIT happens automatically if no exception
    # ROLLBACK happens automatically on exception
```

### Nested Transactions (Savepoints)

```python
async with db.atomic() as conn:
    await conn.execute(
        "INSERT INTO orders (customer_id, total) VALUES ($1, $2)", 1, 100.0
    )

    try:
        async with db.atomic() as inner_conn:
            # This uses a SAVEPOINT
            await inner_conn.execute("INSERT INTO order_items (...) VALUES (...)")
            raise ValueError("Something went wrong")
    except ValueError:
        # Inner transaction rolled back to savepoint
        # Outer transaction continues
        pass

    await conn.execute(
        "UPDATE orders SET status = $1 WHERE id = $2", "partial", order_id
    )
    # COMMIT: the order is saved but the failed item is not
```

### on_commit Callbacks

```python
from hyperdjango.database import on_commit

async with db.atomic() as conn:
    order = await create_order(conn, data)

    # This runs ONLY after the transaction commits successfully
    on_commit(lambda: send_order_confirmation_email(order.id))
```

### select_for_update (Row Locking)

```python
# Lock rows to prevent concurrent modification
product = await Product.objects.filter(id=product_id).select_for_update().get()
# Row is locked until transaction ends
product.stock -= 1
await product.save()
```

---

## Multi-Database Routing

Route queries to different databases (primary/replica):

```python
from hyperdjango.multi_db import ConnectionManager, PrimaryReplicaRouter

connections = ConnectionManager()
connections.configure(
    {
        "default": "postgres://localhost/mydb",
        "replica": "postgres://replica-host/mydb",
    }
)

router = PrimaryReplicaRouter(replica_alias="replica")

# Read from replica
users = await User.objects.using("replica").filter(is_active=True).all()

# Write always goes to primary
await User.objects.create(name="Alice", email="alice@example.com")
```

---

## Query Caching

Transparent caching with version-based invalidation:

```python
# Per-query cache (60 seconds)
products = await Product.objects.filter(is_active=True).cache(60).all()

# Per-model default cache (set in Meta)
class Product(Model):
    class Meta:
        table = "products"
        cache_ttl = 300  # 5 minutes
    ...

# Cache is automatically invalidated when the table is modified
await Product.objects.create(name="New Product", ...)  # Invalidates product cache
```

---

## Migration Notes for Django Users

### Syntax Changes

| Django                       | HyperDjango                                      |
| ---------------------------- | ------------------------------------------------ |
| `Model.objects.all()` (sync) | `await Model.objects.all()` (async)              |
| `Q(x=1) \| Q(y=2)`           | `qs.where_raw("(x = {idx} OR y = {idx})", 1, 2)` |
| `F('price') * 1.1`           | `F("price") * 1.1` (same)                        |
| `annotate(Count('id'))`      | `annotate(count=Count("id"))` (same)             |
| `raw("SELECT ...")`          | `db.query("SELECT ...")`                         |
| `@transaction.atomic`        | `async with db.atomic()`                         |
| `select_for_update()`        | `select_for_update()` (same)                     |

### Behavioral Differences

1. All terminal methods are async (`await`). There are no sync alternatives.
2. Parameters use PostgreSQL `$1, $2` syntax internally (not `%s`).
3. Prepared statement caching is automatic -- repeated queries skip the parse phase.
4. No lazy queryset evaluation. Building the queryset is sync; executing is async.
5. Connection pooling is built into pg.zig with automatic pool management.
6. `COPY` protocol for bulk imports instead of `bulk_create` with individual INSERTs.
