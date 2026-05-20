# Query Cache

Transparent query result caching with version-based invalidation, FK dependency tracking, and signal-driven auto-invalidation. Cache query results in memory or PostgreSQL UNLOGGED tables with zero application code changes via `Meta.cache_ttl` or explicit `.cache()` calls.

## Quick Start

```python
from hyperdjango.query_cache import configure_query_cache
from hyperdjango.cache import LocMemCache

# Configure at app startup
configure_query_cache(
    backend=LocMemCache(max_size=5000),
    default_ttl=60,
    enabled=True,
)

# Use via QuerySet — cached for 120 seconds
users = await User.objects.cache(ttl=120).filter(active=True).all()


# Or set a per-model default TTL
class Product(Model):
    class Meta:
        table = "products"
        cache_ttl = 300  # Cache all queries for 5 minutes

    id: int = Field(primary_key=True, auto=True)
    name: str = Field()
    price: float = Field()
```

## How It Works

### Version-Based Invalidation

Each table has a monotonically increasing version counter maintained in memory. Cache keys embed the current version number, so bumping the version instantly invalidates all cached queries for that table -- no scanning or deletion required.

Cache key format: `qc:{table}:v{version}:{hash}`

When a write occurs on a table (save, delete, update), its version is bumped. All existing cache keys containing the old version automatically miss on lookup because the new key includes the new version number.

```
# Before write: version = 5
Cache key: qc:users:v5:a1b2c3d4

# After write to users table: version → 6
Lookup key: qc:users:v6:a1b2c3d4  ← different key, cache miss
Old entry:  qc:users:v5:a1b2c3d4  ← naturally expires via TTL
```

This gives O(1) invalidation regardless of how many cached queries exist for a table.

### Multi-Table Cache Keys

For queries spanning multiple tables (JOINs), the cache key includes version numbers of ALL involved tables. Any table's version change invalidates the cached result:

```
# Query joining users and orders (users v5, orders v3)
Cache key: qc:orders:v3|users:v5:f8e7d6c5

# Write to orders: orders version → 4
Lookup key: qc:orders:v4|users:v5:f8e7d6c5  ← miss
```

### FK Dependency Tracking

When table A has a foreign key to table B, a write to B also invalidates cached queries for A. This prevents stale JOINed results.

Dependencies are registered automatically when models are defined via `register_model()`. For example, if `Product` has a FK to `Category`:

```
Write to categories table
  → bump categories version (invalidates category queries)
  → bump products version (invalidates product queries that JOIN categories)
```

This cascade is automatic -- you do not need to manually track dependencies.

### Signal-Driven Auto-Invalidation

`post_save` and `post_delete` signals are connected automatically when the `query_cache` module is imported. Any model save or delete triggers cache invalidation for the affected table and all its dependents.

The signal handlers are installed with `dispatch_uid` to prevent duplicate connections:

- `post_save` -> invalidates by row (bumps table version + dependent tables)
- `post_delete` -> invalidates by row (bumps table version + dependent tables)

No manual invalidation is needed for standard ORM operations.

## Configuration

### In-Memory Cache (Default)

```python
from hyperdjango.query_cache import configure_query_cache
from hyperdjango.cache import LocMemCache

configure_query_cache(
    backend=LocMemCache(max_size=10000),  # LRU cache, max 10k entries
    default_ttl=60,  # Default TTL in seconds
    enabled=True,  # Enable/disable globally
)
```

### Database-Backed Cache

For shared cache across processes, use PostgreSQL UNLOGGED tables:

```python
from hyperdjango.cache import DatabaseCache
from hyperdjango.database import get_db

configure_query_cache(
    backend=DatabaseCache(get_db(), table="hyper_cache"),
    default_ttl=300,
)
```

### Configuration Parameters

| Parameter     | Type          | Default                       | Description                        |
| ------------- | ------------- | ----------------------------- | ---------------------------------- |
| `backend`     | cache backend | `LocMemCache(max_size=10000)` | Cache storage backend              |
| `default_ttl` | `int`         | `60`                          | Default cache TTL in seconds       |
| `enabled`     | `bool`        | `True`                        | Enable or disable caching globally |

## Per-Model Cache Configuration

### Meta.cache_ttl

Set a default cache TTL for all queries on a model:

```python
class Product(Model):
    class Meta:
        table = "products"
        cache_ttl = 300  # 5 minutes

    id: int = Field(primary_key=True, auto=True)
    name: str = Field()
    price: float = Field()


class Setting(Model):
    class Meta:
        table = "settings"
        cache_ttl = 3600  # 1 hour — settings change rarely

    key: str = Field(primary_key=True)
    value: str = Field()
```

When `Meta.cache_ttl` is set, all queries on that model are automatically cached without needing `.cache()`:

```python
# Automatically cached for 300 seconds (Meta.cache_ttl)
products = await Product.objects.filter(active=True).all()

# Automatically cached for 3600 seconds
setting = await Setting.objects.get(key="site_name")
```

### Explicit .cache() Override

The `.cache()` QuerySet method overrides `Meta.cache_ttl` for a specific query:

```python
# Override Meta.cache_ttl with explicit TTL
products = await Product.objects.cache(ttl=10).filter(flash_sale=True).all()

# Use Meta.cache_ttl (no .cache() call needed)
products = await Product.objects.filter(active=True).all()
```

### TTL Resolution Order

1. Explicit `.cache(ttl=N)` on the QuerySet (highest priority)
2. `Meta.cache_ttl` on the model class
3. No caching (if neither is set)

## QueryCacheManager API

Access the global cache manager:

```python
from hyperdjango.query_cache import get_query_cache

cache = get_query_cache()
```

### Cache Key Generation

```python
# Single-table query key
key = cache.make_key("users", sql, params)
# Returns: "qc:users:v{version}:{hash}"

# Multi-table query key (JOINs)
key = cache.make_multi_table_key(["users", "orders"], sql, params)
# Returns: "qc:orders:v{version}|users:v{version}:{hash}"
```

The hash is an MD5 digest of the SQL + params, truncated to 16 characters. The version numbers are embedded in the key for instant invalidation.

### Get / Set

```python
# Get a cached result (returns None on miss)
result = cache.get(key)

# Cache a result with TTL
cache.set(key, value, ttl=120)

# Cache with default TTL
cache.set(key, value)  # Uses default_ttl from configuration
```

### Invalidation

```python
# Invalidate all cached queries for a table
# Also cascades to tables with FK dependencies
cache.invalidate_table("users")

# Invalidate queries involving a specific row
# Under version-based caching, this bumps the table version
cache.invalidate_row("users", 42)

# Invalidate everything (all tables, all queries)
cache.invalidate_all()
```

### Table-Level Invalidation Cascade

When you invalidate a table, all dependent tables are also invalidated:

```python
# Given: Product has FK to Category
# invalidate_table("categories") bumps:
#   - categories version (invalidates category queries)
#   - products version (invalidates product queries)
cache.invalidate_table("categories")
```

### Row-Level Invalidation

Row-level invalidation bumps the table version (since version-keyed entries cannot be selectively invalidated) but tracks the event separately in statistics for monitoring:

```python
cache.invalidate_row("users", 42)
# Bumps users version + dependent table versions
# Stats: row_invalidations += 1
```

### Cache Control

```python
# Disable caching globally (queries bypass cache)
cache.enabled = False

# Re-enable caching
cache.enabled = True

# Clear all cached data and reset version counters
cache.clear()
```

## Cache Warming

Pre-populate the cache with known-hot queries:

```python
cache = get_query_cache()

# Warm a specific cache entry
cache.warm(key, value, ttl=300)


# Warm pattern: run queries at startup
async def warm_cache():
    """Pre-populate cache with frequently accessed data."""
    # These queries will be cached automatically
    await Product.objects.cache(ttl=600).filter(featured=True).all()
    await Category.objects.cache(ttl=3600).all()
    await Setting.objects.cache(ttl=3600).all()
```

### Startup Cache Warming

```python
from hyperdjango import HyperApp

app = HyperApp()


@app.on_startup
async def startup():
    configure_query_cache(
        backend=LocMemCache(max_size=10000),
        default_ttl=60,
    )
    await warm_cache()
```

## Cache Statistics

Track cache performance with built-in statistics:

```python
cache = get_query_cache()
stats = cache.stats

# Hit/miss tracking
stats.hits  # Number of cache hits
stats.misses  # Number of cache misses
stats.hit_rate  # Hit ratio (0.0 to 1.0)
stats.total_requests  # hits + misses

# Write tracking
stats.sets  # Number of cache writes

# Invalidation tracking
stats.invalidations  # Total invalidation events
stats.table_invalidations  # Table-level invalidation count
stats.row_invalidations  # Row-level invalidation count

# Reset all counters
stats.reset()
```

### Monitoring Cache Performance

```python
@app.route("/admin/cache-stats")
async def cache_stats_view(request):
    cache = get_query_cache()
    stats = cache.stats
    return Response.json(
        {
            "hit_rate": f"{stats.hit_rate:.1%}",
            "hits": stats.hits,
            "misses": stats.misses,
            "total_requests": stats.total_requests,
            "sets": stats.sets,
            "invalidations": stats.invalidations,
            "table_invalidations": stats.table_invalidations,
            "row_invalidations": stats.row_invalidations,
            "table_versions": cache.get_table_versions(),
        }
    )
```

### Interpreting Hit Rate

| Hit Rate | Interpretation | Action                                                    |
| -------- | -------------- | --------------------------------------------------------- |
| > 90%    | Excellent      | Cache is working well                                     |
| 70-90%   | Good           | Consider increasing TTL for stable data                   |
| 50-70%   | Moderate       | Review invalidation patterns, may be too aggressive       |
| < 50%    | Poor           | Data changes too frequently for caching, or TTL too short |

## Cache Bypass for Fresh Queries

When you need guaranteed-fresh data, bypass the cache:

```python
# Option 1: Temporarily disable caching
cache = get_query_cache()
cache.enabled = False
fresh_data = await User.objects.filter(active=True).all()
cache.enabled = True


# Option 2: Don't use .cache() and don't set Meta.cache_ttl
# Queries without caching configuration are never cached
class TransientData(Model):
    class Meta:
        table = "transient_data"
        # No cache_ttl — queries always hit the database


# Option 3: Invalidate before reading
cache.invalidate_table("users")
fresh_users = await User.objects.cache(ttl=60).filter(active=True).all()
```

## Manual Invalidation

For bulk operations or raw SQL that bypasses ORM signals:

```python
from hyperdjango.query_cache import get_query_cache
from hyperdjango.database import get_db

cache = get_query_cache()
db = get_db()

# Raw SQL update (doesn't trigger post_save signal)
await db.execute(
    "UPDATE products SET price = price * 1.1 WHERE category_id = $1", (cat_id,)
)

# Manually invalidate since signals didn't fire
cache.invalidate_table("products")

# Bulk delete via raw SQL
await db.execute("DELETE FROM sessions WHERE expires_at < NOW()")
cache.invalidate_table("sessions")
```

## Dependency Registration

FK dependencies are registered automatically when models are defined. For manual registration (custom dependencies, views, etc.):

```python
cache = get_query_cache()

# Register: changes to "categories" should invalidate "products" cache
cache.dependencies.register_dependency("products", "categories")

# Query dependencies
dependents = cache.dependencies.get_dependents("categories")
# Returns: {"products"}

# Get all affected tables (table + dependents)
affected = cache.dependencies.get_all_affected_tables("categories")
# Returns: {"categories", "products"}

# Clear all registered dependencies
cache.dependencies.clear()
```

### Automatic FK Registration

When a model with foreign keys is registered with the query cache, dependencies are auto-detected:

```python
class Category(Model):
    class Meta:
        table = "categories"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field()


class Product(Model):
    class Meta:
        table = "products"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field()
    category_id: int = Field(foreign_key=Category)


# When Product is registered:
# cache.dependencies.register_dependency("products", "categories")
# Now: write to categories → invalidates both categories AND products cache
```

## Invalidation Cascade Example

A complete walkthrough of how invalidation cascades work:

```python
# Models
class Category(Model):
    class Meta:
        table = "categories"
        cache_ttl = 300

    id: int = Field(primary_key=True, auto=True)
    name: str = Field()


class Product(Model):
    class Meta:
        table = "products"
        cache_ttl = 300

    id: int = Field(primary_key=True, auto=True)
    name: str = Field()
    category_id: int = Field(foreign_key=Category)


# Step 1: Query products (cached at products v0)
products = await Product.objects.filter(category_id=1).all()
# Cache key: qc:products:v0:{hash}

# Step 2: Query categories (cached at categories v0)
categories = await Category.objects.all()
# Cache key: qc:categories:v0:{hash}

# Step 3: Update a category
category = await Category.objects.get(id=1)
category.name = "Updated Name"
await category.save()
# post_save signal fires:
#   1. categories version bumped: 0 → 1
#   2. products version bumped: 0 → 1 (FK dependency)

# Step 4: Query products again (cache miss — version changed)
products = await Product.objects.filter(category_id=1).all()
# New cache key: qc:products:v1:{hash} — different from v0, so cache miss
# Fresh data fetched from database and cached at v1
```

## Global Access

```python
from hyperdjango.query_cache import (
    get_query_cache,
    set_query_cache,
    configure_query_cache,
    QueryCacheManager,
)

# Get the global singleton (creates a default if none configured)
cache = get_query_cache()

# Replace with a custom instance
manager = QueryCacheManager(backend=my_backend, default_ttl=120)
set_query_cache(manager)

# Configure with convenience function (creates and sets)
cache = configure_query_cache(backend=my_backend, default_ttl=120)
```

## Table Version Diagnostics

Inspect current version counters to understand invalidation patterns:

```python
cache = get_query_cache()

# Get all table version counters
versions = cache.get_table_versions()
# {"users": 5, "products": 12, "categories": 3, "orders": 47}

# High version numbers indicate frequent writes (and frequent invalidation)
for table, version in sorted(versions.items(), key=lambda x: -x[1]):
    print(f"{table}: v{version}")
```

Tables with very high version numbers relative to others may not benefit from caching (too many invalidations). Consider:

- Removing `cache_ttl` for high-churn tables
- Using shorter TTLs for moderate-churn tables
- Using longer TTLs for low-churn reference data

## Practical Patterns

### Reference Data Caching

Cache rarely-changing reference data with long TTLs:

```python
class Country(Model):
    class Meta:
        table = "countries"
        cache_ttl = 86400  # 24 hours


class Currency(Model):
    class Meta:
        table = "currencies"
        cache_ttl = 3600  # 1 hour


class Setting(Model):
    class Meta:
        table = "settings"
        cache_ttl = 600  # 10 minutes
```

### Hot Query Caching

Cache specific expensive queries:

```python
# Dashboard stats — cached 5 minutes
stats = await Order.objects.cache(ttl=300).aggregate(
    total=Count("id"),
    revenue=Sum("amount"),
)

# Product listing — cached 1 minute
products = (
    await Product.objects.cache(ttl=60)
    .filter(
        active=True,
    )
    .order_by("-created_at")
    .limit(50)
)
```

### Cache-Aside for Complex Queries

For queries involving raw SQL or complex logic, use the cache API directly:

```python
cache = get_query_cache()

key = cache.make_key("dashboard", "custom_stats_query", ())
result = cache.get(key)

if result is None:
    db = get_db()
    result = await db.query("""
        SELECT
            date_trunc('day', created_at) as day,
            COUNT(*) as orders,
            SUM(amount) as revenue
        FROM orders
        WHERE created_at > NOW() - INTERVAL '30 days'
        GROUP BY 1
        ORDER BY 1
    """)
    cache.set(key, result, ttl=300)
```

## Reference

### Imports

```python
from hyperdjango.query_cache import (
    QueryCacheManager,  # Cache manager class
    CacheStats,  # Statistics dataclass
    DependencyTracker,  # FK dependency tracker
    get_query_cache,  # Get global singleton
    set_query_cache,  # Set global singleton
    configure_query_cache,  # Configure and set
)
```

### QueryCacheManager API

| Method                                      | Returns             | Description                     |
| ------------------------------------------- | ------------------- | ------------------------------- |
| `make_key(table, sql, params)`              | `str`               | Generate versioned cache key    |
| `make_multi_table_key(tables, sql, params)` | `str`               | Multi-table versioned key       |
| `get(key)`                                  | `Any \| None`       | Get cached result               |
| `set(key, value, ttl)`                      | None                | Cache a result                  |
| `invalidate_table(table)`                   | None                | Invalidate table + dependents   |
| `invalidate_row(table, pk)`                 | None                | Invalidate by row               |
| `invalidate_all()`                          | None                | Invalidate everything           |
| `register_model(model_class)`               | None                | Register FK dependencies        |
| `warm(key, value, ttl)`                     | None                | Pre-populate cache              |
| `get_table_versions()`                      | `dict[str, int]`    | Current version counters        |
| `clear()`                                   | None                | Clear all data + reset versions |
| `enabled`                                   | `bool`              | Get/set enabled state           |
| `stats`                                     | `CacheStats`        | Access statistics               |
| `dependencies`                              | `DependencyTracker` | Access dependency tracker       |

### CacheStats Fields

| Field                 | Type    | Description               |
| --------------------- | ------- | ------------------------- |
| `hits`                | `int`   | Cache hit count           |
| `misses`              | `int`   | Cache miss count          |
| `hit_rate`            | `float` | `hits / (hits + misses)`  |
| `total_requests`      | `int`   | `hits + misses`           |
| `sets`                | `int`   | Cache write count         |
| `invalidations`       | `int`   | Total invalidation events |
| `table_invalidations` | `int`   | Table-level invalidations |
| `row_invalidations`   | `int`   | Row-level invalidations   |
| `reset()`             | None    | Reset all counters to 0   |

### DependencyTracker API

| Method                                | Returns    | Description                  |
| ------------------------------------- | ---------- | ---------------------------- |
| `register_dependency(source, target)` | None       | source has FK to target      |
| `get_dependents(table)`               | `set[str]` | Tables with FK to this table |
| `get_all_affected_tables(table)`      | `set[str]` | Table + all dependents       |
| `clear()`                             | None       | Clear all dependencies       |
