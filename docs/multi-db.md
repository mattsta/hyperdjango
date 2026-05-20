# Multi-Database Routing

Route queries to different PostgreSQL instances -- read replicas, per-model databases, or explicit selection. HyperDjango's multi-database system supports automatic routing via `DatabaseRouter`, per-model binding via `Meta.database`, and explicit selection via `QuerySet.using()`.

## Quick Start

```python
from hyperdjango.multi_db import ConnectionManager, set_connections

connections = ConnectionManager()
await connections.configure(
    {
        "default": "postgres://localhost/myapp",
        "replica": "postgres://replica-host/myapp",
        "analytics": "postgres://analytics-host/warehouse",
    }
)
set_connections(connections)
```

## Connection Manager

`ConnectionManager` is the central registry for named database connections. Each connection is a `Database` instance with its own connection pool.

### Configuration

Pass a dictionary of name-to-URL mappings. Each URL creates a `Database` instance and connects immediately:

```python
from hyperdjango.multi_db import ConnectionManager, set_connections

connections = ConnectionManager()

# Simple URL strings
await connections.configure(
    {
        "default": "postgres://localhost/myapp",
        "replica": "postgres://replica-host/myapp",
    }
)

# Or config dicts with pool sizing
await connections.configure(
    {
        "default": {
            "url": "postgres://localhost/myapp",
            "min_size": 5,
            "max_size": 20,
        },
        "replica": {
            "url": "postgres://replica-host/myapp",
            "min_size": 2,
            "max_size": 10,
        },
        "analytics": {
            "url": "postgres://analytics-host/warehouse",
            "min_size": 1,
            "max_size": 5,
        },
    }
)

# Register as global singleton
set_connections(connections)
```

When a "default" connection is configured, it is automatically set as the global database via `set_db()`. This means all queries without explicit routing use the "default" connection.

### Config Dict Keys

| Key        | Type  | Default  | Description               |
| ---------- | ----- | -------- | ------------------------- |
| `url`      | `str` | required | PostgreSQL connection URL |
| `min_size` | `int` | 2        | Minimum pool connections  |
| `max_size` | `int` | 10       | Maximum pool connections  |

### Accessing Connections

```python
# Dictionary-style access (raises KeyError if not found)
default_db = connections["default"]
replica_db = connections["replica"]

# .get() with optional default (returns None if not found)
analytics_db = connections.get("analytics")
missing_db = connections.get("missing")  # None

# Check if a connection exists
if "replica" in connections:
    ...

# Get all configured databases
all_dbs = connections.databases  # dict[str, Database]
```

### Closing All Connections

```python
# Disconnect all databases and clear the registry
await connections.close_all()
```

## QuerySet.using()

Explicitly select which database to query. This is the highest-priority routing mechanism:

```python
# Read from replica
users = await User.objects.using("replica").filter(active=True).all()

# Write to default (primary)
await User.objects.using("default").create(name="Alice")

# Run analytics queries against a dedicated database
events = (
    await AnalyticsEvent.objects.using("analytics")
    .filter(
        event_type="purchase",
        created_at__gte="2024-01-01",
    )
    .all()
)
```

### Using a Database Instance Directly

You can pass a `Database` instance instead of a string alias:

```python
from hyperdjango.database import Database

db = Database("postgres://other-host/otherdb")
await db.connect()

users = await User.objects.using(db).all()
```

### Chaining with Other QuerySet Methods

`.using()` returns a new QuerySet and chains with all other methods:

```python
# Complex query on replica
top_authors = (
    await Author.objects.using("replica")
    .annotate(
        article_count=Count("articles"),
    )
    .filter(
        article_count__gt=10,
    )
    .order_by("-article_count")
    .limit(20)
)
```

## Database Router

Route reads and writes automatically by implementing a `DatabaseRouter` subclass. Return a database alias string, or `None` to fall through to the default:

```python
from hyperdjango.multi_db import DatabaseRouter


class MyRouter(DatabaseRouter):
    def db_for_read(self, model):
        """Return database alias for reads, or None for default."""
        return "replica"

    def db_for_write(self, model):
        """Return database alias for writes, or None for default."""
        return "default"


connections.router = MyRouter()

# Now all reads go to replica, writes to default — no .using() needed
users = await User.objects.filter(active=True).all()  # reads from replica
await User.objects.create(name="Alice")  # writes to default
```

### DatabaseRouter Protocol

The base `DatabaseRouter` class defines four methods. Override any combination:

```python
class DatabaseRouter:
    def db_for_read(self, model) -> str | None:
        """Return database alias for read queries, or None for default."""
        return None

    def db_for_write(self, model) -> str | None:
        """Return database alias for write queries, or None for default."""
        return None

    def allow_relation(self, obj1, obj2) -> bool | None:
        """Allow or deny cross-database relations.

        Return True to allow, False to deny, None to defer to next router.
        """
        return None

    def allow_migrate(self, db: str, model) -> bool | None:
        """Allow or deny running migrations on a specific database.

        Return True to allow, False to deny, None to defer.
        """
        return None
```

### Model-Specific Routing

Route different models to different databases based on the model class:

```python
class ShardRouter(DatabaseRouter):
    def db_for_read(self, model):
        if model.__name__ == "AnalyticsEvent":
            return "analytics"
        if model.__name__ in ("AuditLog", "SecurityEvent"):
            return "audit_db"
        return "replica"

    def db_for_write(self, model):
        if model.__name__ == "AnalyticsEvent":
            return "analytics"
        if model.__name__ in ("AuditLog", "SecurityEvent"):
            return "audit_db"
        return "default"

    def allow_migrate(self, db, model):
        # Only migrate analytics models on the analytics database
        if model.__name__ == "AnalyticsEvent":
            return db == "analytics"
        if model.__name__ in ("AuditLog", "SecurityEvent"):
            return db == "audit_db"
        # All other models on default
        return db == "default"
```

### App-Based Routing

Route based on module path or app label:

```python
class AppRouter(DatabaseRouter):
    ANALYTICS_MODELS = {"AnalyticsEvent", "PageView", "Conversion"}
    LOGGING_MODELS = {"AuditLog", "SecurityEvent", "AccessLog"}

    def _get_db(self, model):
        name = model.__name__
        if name in self.ANALYTICS_MODELS:
            return "analytics"
        if name in self.LOGGING_MODELS:
            return "logging"
        return None  # Fall through to default

    def db_for_read(self, model):
        return self._get_db(model)

    def db_for_write(self, model):
        return self._get_db(model)
```

## Built-in: PrimaryReplicaRouter

The most common pattern -- route all reads to a replica, all writes to the primary:

```python
from hyperdjango.multi_db import PrimaryReplicaRouter

connections.router = PrimaryReplicaRouter(
    replica="replica",  # default: "replica"
    primary="default",  # default: "default"
)

# All reads go to replica
users = await User.objects.filter(active=True).all()  # replica

# All writes go to primary
await User.objects.create(name="Alice")  # default
```

### Custom Replica Names

```python
# If your replica has a different alias
connections.router = PrimaryReplicaRouter(
    replica="read_replica_1",
    primary="primary_db",
)
```

## Per-Model Database Binding

Bind a model to a specific database via `Meta.database`. This is useful when a model's data always lives on a specific database:

```python
class AnalyticsEvent(Model):
    class Meta:
        table = "events"
        database = "analytics"  # Always uses the "analytics" connection

    id: int = Field(primary_key=True, auto=True)
    event_type: str = Field()
    payload: str = Field()
    created_at: datetime = Field()


class AuditLog(Model):
    class Meta:
        table = "audit_log"
        database = "audit_db"

    id: int = Field(primary_key=True, auto=True)
    action: str = Field()
    details: str = Field()
```

Per-model binding takes priority over router configuration. No `.using()` call needed:

```python
# Automatically uses "analytics" database
events = await AnalyticsEvent.objects.filter(event_type="purchase").all()

# Automatically uses "audit_db" database
logs = await AuditLog.objects.filter(action="login").all()
```

## Resolution Priority

When resolving which database to use for a query, the system checks in this order:

1. **`QuerySet.using("name")`** -- explicit selection (highest priority)
2. **`Meta.database = "name"`** -- per-model binding
3. **`DatabaseRouter.db_for_read/write()`** -- router-based routing
4. **`"default"`** -- fallback to the default connection

```python
# Example: model has Meta.database = "analytics", router says "replica"

# .using() wins over everything
await Event.objects.using("default").all()  # Uses "default"

# Meta.database wins over router
await Event.objects.all()  # Uses "analytics" (not "replica")

# Router is used when no Meta.database and no .using()
await User.objects.all()  # Uses router's db_for_read() result
```

### Resolution Methods

`ConnectionManager` exposes the resolution logic:

```python
# Resolve read database for a model
db = connections.resolve_for_read(User)  # Checks Meta → Router → default
db = connections.resolve_for_write(User)  # Checks Meta → Router → default
```

## Sticky Connections for Writes

A common challenge with read replicas is replication lag -- after writing to the primary, an immediate read from a replica may return stale data. The sticky connection pattern routes reads to the primary for a short window after any write:

```python
import time


class StickyPrimaryReplicaRouter(DatabaseRouter):
    """Route reads to replica, but stick to primary briefly after writes."""

    def __init__(self, replica="replica", primary="default", sticky_seconds=2.0):
        self.replica = replica
        self.primary = primary
        self.sticky_seconds = sticky_seconds
        self._last_write: float = 0.0

    def db_for_read(self, model):
        # If a write happened recently, read from primary to avoid stale data
        if time.monotonic() - self._last_write < self.sticky_seconds:
            return self.primary
        return self.replica

    def db_for_write(self, model):
        self._last_write = time.monotonic()
        return self.primary


connections.router = StickyPrimaryReplicaRouter(sticky_seconds=2.0)
```

### Per-Request Sticky Routing

For web applications, you typically want sticky routing scoped to the current request:

```python
import contextvars

_wrote_in_request = contextvars.ContextVar("wrote_in_request", default=False)


class RequestStickyRouter(DatabaseRouter):
    """Sticky to primary for the rest of the request after any write."""

    def __init__(self, replica="replica", primary="default"):
        self.replica = replica
        self.primary = primary

    def db_for_read(self, model):
        if _wrote_in_request.get():
            return self.primary
        return self.replica

    def db_for_write(self, model):
        _wrote_in_request.set(True)
        return self.primary


# Reset at the start of each request via middleware
async def sticky_reset_middleware(request, call_next):
    _wrote_in_request.set(False)
    return await call_next(request)
```

## Connection Health Monitoring

Monitor connection pool health across all databases:

```python
async def check_all_connections():
    """Health check for all configured databases."""
    results = {}
    for name, db in connections.databases.items():
        try:
            rows = await db.query("SELECT 1")
            results[name] = {"status": "healthy", "pool_size": db.pool_size}
        except Exception as e:
            results[name] = {"status": "unhealthy", "error": str(e)}
    return results
```

### Health Check Endpoint

```python
@app.route("/health/db")
async def db_health(request: Request) -> Response:
    health = await check_all_connections()
    all_healthy = all(r["status"] == "healthy" for r in health.values())
    status = 200 if all_healthy else 503
    return Response.json(health, status=status)
```

## Router Composition

For complex routing needs, compose multiple routing strategies:

```python
class CompositeRouter(DatabaseRouter):
    """Chain multiple routing strategies."""

    def __init__(self):
        self.model_routes = {
            "AnalyticsEvent": "analytics",
            "AuditLog": "audit_db",
        }
        self.sticky = StickyPrimaryReplicaRouter()

    def db_for_read(self, model):
        # First check model-specific routes
        name = model.__name__
        if name in self.model_routes:
            return self.model_routes[name]
        # Fall back to sticky primary/replica
        return self.sticky.db_for_read(model)

    def db_for_write(self, model):
        name = model.__name__
        if name in self.model_routes:
            return self.model_routes[name]
        return self.sticky.db_for_write(model)

    def allow_migrate(self, db, model):
        name = model.__name__
        if name in self.model_routes:
            return db == self.model_routes[name]
        return db == "default"
```

## Cross-Database Relations

The `allow_relation` method controls whether two model instances from different databases can have a foreign key relationship:

```python
class StrictRouter(DatabaseRouter):
    def allow_relation(self, obj1, obj2):
        # Only allow relations between objects on the same database
        db1 = obj1._meta.database or "default"
        db2 = obj2._meta.database or "default"
        if db1 == db2:
            return True
        return False
```

## Global Connection Manager

Access the global connection manager singleton:

```python
from hyperdjango.multi_db import get_connections, set_connections

# Get the global manager (creates empty one if not configured)
connections = get_connections()

# Replace with a custom instance
custom = ConnectionManager()
await custom.configure({...})
set_connections(custom)
```

## Practical Examples

### E-Commerce with Read Replicas

```python
connections = ConnectionManager()
await connections.configure(
    {
        "default": "postgres://primary/ecommerce",
        "replica": "postgres://replica/ecommerce",
        "analytics": "postgres://analytics/warehouse",
    }
)

connections.router = PrimaryReplicaRouter()
set_connections(connections)


class ProductView:
    async def get(self, request, product_id):
        # Reads from replica automatically
        product = await Product.objects.get(id=product_id)
        return Response.json(product.to_dict())

    async def post(self, request, product_id):
        # Writes to primary automatically
        product = await Product.objects.get(id=product_id)
        product.stock -= 1
        await product.save()
        return Response.json({"status": "ok"})
```

### Multi-Tenant with Separate Databases

```python
class TenantRouter(DatabaseRouter):
    """Route based on a context variable set by middleware."""

    def db_for_read(self, model):
        tenant = current_tenant.get()
        return f"tenant_{tenant}" if tenant else "default"

    def db_for_write(self, model):
        tenant = current_tenant.get()
        return f"tenant_{tenant}" if tenant else "default"


# Middleware sets the tenant context
async def tenant_middleware(request, call_next):
    tenant = request.headers.get("X-Tenant-ID", "default")
    token = current_tenant.set(tenant)
    try:
        return await call_next(request)
    finally:
        current_tenant.reset(token)
```

### Migration Control

Control which models can migrate on which databases:

```python
class MigrationRouter(DatabaseRouter):
    def allow_migrate(self, db, model):
        if model.__name__ == "AnalyticsEvent":
            return db == "analytics"
        if model.__name__ == "AuditLog":
            return db == "audit_db"
        # Default models only on default
        return db == "default"
```

## Reference

### Imports

```python
from hyperdjango.multi_db import (
    ConnectionManager,  # Named database pool registry
    DatabaseRouter,  # Base router class
    PrimaryReplicaRouter,  # Built-in read/write splitter
    get_connections,  # Get global ConnectionManager
    set_connections,  # Set global ConnectionManager
)
```

### ConnectionManager API

| Method                       | Returns               | Description                              |
| ---------------------------- | --------------------- | ---------------------------------------- |
| `await configure(databases)` | None                  | Configure and connect all databases      |
| `[name]`                     | `Database`            | Get connection by name (raises KeyError) |
| `get(name, default)`         | `Database \| None`    | Get connection or default                |
| `name in connections`        | `bool`                | Check if connection exists               |
| `databases`                  | `dict[str, Database]` | All configured connections               |
| `resolve_for_read(model)`    | `Database`            | Resolve read database                    |
| `resolve_for_write(model)`   | `Database`            | Resolve write database                   |
| `await close_all()`          | None                  | Disconnect all databases                 |

### DatabaseRouter API

| Method                       | Returns        | Description               |
| ---------------------------- | -------------- | ------------------------- |
| `db_for_read(model)`         | `str \| None`  | Database alias for reads  |
| `db_for_write(model)`        | `str \| None`  | Database alias for writes |
| `allow_relation(obj1, obj2)` | `bool \| None` | Allow cross-db FK         |
| `allow_migrate(db, model)`   | `bool \| None` | Allow migration on db     |
