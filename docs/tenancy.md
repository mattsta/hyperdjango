# Multi-Tenant & Hierarchical Ownership

Zero-intrusion multi-tenant data isolation. Add one mixin to your model, one middleware to your app — queries auto-filter, admin auto-scopes, permissions auto-bound.

## Quick Start

```python
from hyperdjango import HyperApp
from hyperdjango.models import Field, Model
from hyperdjango.tenancy import TenantMixin, TenantMiddleware, resolve_from_user


# 1. Add TenantMixin to your model
class Post(TenantMixin, Model):
    class Meta:
        table = "posts"

    title: str = Field()
    body: str = Field()


# 2. Add TenantMiddleware to your app
app = HyperApp(database="postgres://localhost/mydb")
app.use(TenantMiddleware(resolve_tenant=resolve_from_user))

# That's it. Every Post query now auto-filters by tenant.
```

## How It Works

When a request arrives:

1. **TenantMiddleware** calls your resolver to extract the tenant from the request
2. The tenant identity is stored in a **`contextvars.ContextVar`** (async-safe, per-request)
3. Every **QuerySet** on a `TenantMixin` model auto-injects `WHERE tenant_id = $N`
4. On **save**, the `pre_save` signal **forces** `tenant_id` to the context tenant,
   overriding any user-supplied value (mass-assignment defense)
5. **Admin** views auto-scope list, search, filter, and autocomplete queries

**Fail closed (`TENANT_STRICT`, default `True`).** A `TenantMixin` query with **no
active tenant context** returns **zero rows** (not every tenant's data), and a
create with no context and no explicit `tenant_id` is refused. Cross-tenant /
no-context access (CLI, migrations, admin, services) must be explicit via
`.unscoped()` or by establishing `tenant_context(...)`. Set `TENANT_STRICT=False`
to allow the "no context → global" behaviour for non-security apps.

## Models

### TenantMixin — Single Tenant

```python
from hyperdjango.tenancy import TenantMixin


class Post(TenantMixin, Model):
    class Meta:
        table = "posts"

    title: str = Field()
    author_id: int = Field()


class Comment(TenantMixin, Model):
    class Meta:
        table = "comments"

    post_id: int = Field(foreign_key=Post)
    body: str = Field()
```

`TenantMixin` adds:

- `tenant_id: int` field (indexed)
- Auto-scoped `TenantQuerySet` via `_queryset_class`

### Composing with Other Mixins

```python
from hyperdjango.mixins import SoftDeleteMixin, TimestampMixin, OwnershipMixin


class Post(TenantMixin, SoftDeleteMixin, TimestampMixin, OwnershipMixin, Model):
    class Meta:
        table = "posts"

    title: str = Field()
```

All mixins compose via MRO. The QuerySet inherits from all custom QuerySets:

- `TenantQuerySet._build_where_tree()` adds `tenant_id = {}` WhereNode child
- `SoftDeleteQuerySet._build_where_tree()` adds `is_deleted = FALSE` WhereNode child
- Each calls `super()._build_where_tree()` so they chain correctly via MRO
- Cache fast-path: `_mixin_cache_key()` + `_collect_mixin_params()` skip tree on cache hit

### HierarchicalTenantMixin — Org/Team/Project Trees

```python
from hyperdjango.tenancy import HierarchicalTenantMixin


class Team(HierarchicalTenantMixin, Model):
    class Meta:
        table = "teams"

    name: str = Field()
    # parent_tenant_id automatically added — points to parent org
```

## Tenant Resolution

### Built-in Resolvers

| Resolver                          | Source                    | Usage                                     |
| --------------------------------- | ------------------------- | ----------------------------------------- |
| `resolve_from_user`               | `request.user.tenant_id`  | Most common. Requires SessionAuth first.  |
| `resolve_from_header`             | `X-Tenant-ID` header      | API gateways that inject tenant identity. |
| `resolve_from_url`                | `/t/{tenant_id}/...` path | Multi-tenant URL routing.                 |
| `make_subdomain_resolver(lookup)` | Subdomain → DB lookup     | `acme.app.com` → query tenant table.      |

### resolve_from_user (recommended)

```python
from hyperdjango.tenancy import TenantMiddleware, resolve_from_user

# User model must have tenant_id field
app.use(SessionAuth(secret=..., store=store, db=app.db))  # Must run first
app.use(TenantMiddleware(resolve_tenant=resolve_from_user))
```

### resolve_from_header

```python
from hyperdjango.tenancy import TenantMiddleware, resolve_from_header

app.use(TenantMiddleware(resolve_tenant=resolve_from_header))
# Client sends: X-Tenant-ID: 42
```

**Security note:** Only use behind a trusted API gateway that validates the header. Do not expose to public clients.

### make_subdomain_resolver

```python
from hyperdjango.tenancy import TenantMiddleware, make_subdomain_resolver


async def lookup_tenant_by_slug(slug: str) -> int | None:
    row = await db.query_one(
        "SELECT id FROM hyper_tenants WHERE slug = $1 AND is_active = TRUE", slug
    )
    return row["id"] if row else None


resolver = make_subdomain_resolver(lookup_tenant_by_slug)
app.use(TenantMiddleware(resolve_tenant=resolver))
# acme.app.com → lookup("acme") → tenant_id=42
```

### Custom Resolver

```python
from hyperdjango.tenancy import TenantMiddleware, TenantRef


def my_resolver(request) -> TenantRef | None:
    # Your custom logic — API key lookup, JWT claim, cookie, etc.
    api_key = request.headers.get("authorization", "")
    tenant_id = lookup_tenant_from_api_key(api_key)
    if tenant_id is None:
        return None
    return TenantRef(tenant_id=tenant_id)


app.use(TenantMiddleware(resolve_tenant=my_resolver))
```

## Query Scoping

### Automatic (default)

```python
# With tenant context active (tenant_id=42):
posts = await Post.objects.filter(status="published").all()
# SQL: SELECT ... FROM posts WHERE tenant_id = 42 AND status = 'published'

count = await Post.objects.filter(status="draft").count()
# SQL: SELECT COUNT(*) FROM posts WHERE tenant_id = 42 AND status = 'draft'
```

### Escape Hatch — .unscoped()

```python
# Cross-tenant query (admin dashboard, migration, analytics):
all_posts = await Post.objects.unscoped().all()
# SQL: SELECT ... FROM posts (no tenant filter)

# Unscoped + other filters still work:
recent = (
    await Post.objects.unscoped()
    .filter(status="published")
    .order_by("-created_at")
    .limit(100)
)
```

### Background Tasks — tenant_context()

```python
from hyperdjango.tenancy import tenant_context


# Background task with explicit tenant:
async def send_digest(tenant_id: int):
    with tenant_context(tenant_id=tenant_id):
        posts = await Post.objects.filter(status="published").all()
        # Scoped to this tenant
        await send_email(posts)
```

### No Context (CLI, migrations, services, background jobs)

With `TENANT_STRICT` (default), a tenant model queried with no active context
fails **closed** — you must be explicit about crossing tenants:

```python
# Cross-tenant / global (admin, migration, analytics): opt out explicitly.
posts = await Post.objects.unscoped().all()  # sees all tenants

# Scope a service/job to one tenant: establish the context.
with tenant_context(tenant_id=42):
    posts = await Post.objects.all()  # WHERE tenant_id = 42

# Neither of the above, no context → fails closed (returns nothing), so a
# forgotten scope can never leak another tenant's rows:
posts = await Post.objects.all()  # SQL: ... WHERE 1 = 0
```

## Ownership Enforcement on Save

When saving a `TenantMixin` instance inside a tenant context, `tenant_id` is
**forced** to the context tenant via the `pre_save` signal:

```python
with tenant_context(tenant_id=42):
    post = Post(title="Hello")
    await post.save()
    assert post.tenant_id == 42  # set from context
```

A user-supplied `tenant_id` is **overridden**, not preserved — this is the
mass-assignment defense that stops a create bound from a request body
(`Post(**request.json())` with a forged `tenant_id`) from writing into another
tenant's space:

```python
with tenant_context(tenant_id=42):
    post = Post(title="Hello", tenant_id=99)  # forged / mass-assigned
    await post.save()
    assert post.tenant_id == 42  # forced to the context tenant, NOT 99
```

Cross-tenant seeding (CLI/admin, no active context) sets `tenant_id` explicitly
and is allowed; a create with neither a context nor an explicit `tenant_id` is
refused under `TENANT_STRICT`.

## Admin Integration

Admin list views, search, autocomplete, and filter queries are automatically tenant-scoped when the model uses `TenantMixin`. No admin code changes needed.

```python
from hyperdjango.admin import HyperAdmin

admin = HyperAdmin(app)
admin.register(Post, search_fields=["title"], list_filter=["status"])
# List view auto-filters by current tenant
# Autocomplete auto-filters FK targets that use TenantMixin
```

## Tenant-Scoped Permissions

The RBAC system supports tenant-scoped permissions. A permission can be global (`tenant_id IS NULL`) or tenant-specific:

```python
from hyperdjango.auth.permissions import PermissionChecker

checker = PermissionChecker(db)

# Grant a permission within a specific tenant
await checker.grant_tenant_perm(
    user_id=5, codename="add_post", model_name="post", tenant_id=42
)

# Check — uses current tenant context by default
with tenant_context(tenant_id=42):
    can_add = await checker.has_tenant_perm(user, "add_post", "post")
    # True — user has add_post in tenant 42

with tenant_context(tenant_id=99):
    can_add = await checker.has_tenant_perm(user, "add_post", "post")
    # False — user does NOT have add_post in tenant 99
```

Global permissions (no tenant_id) always apply regardless of tenant context.

## Optional Tenant Model

For apps that need a tenant table with hierarchical support:

```python
from hyperdjango.tenancy import Tenant, get_tenant_hierarchy

# Create tables
await db.execute(CREATE_TENANTS_TABLE_SQL)

# Insert hierarchy: Org → Team → Project
await db.execute(
    "INSERT INTO hyper_tenants (id, name, slug) VALUES (1, 'Acme Corp', 'acme')"
)
await db.execute(
    "INSERT INTO hyper_tenants (id, name, slug, parent_id) VALUES (2, 'Engineering', 'eng', 1)"
)
await db.execute(
    "INSERT INTO hyper_tenants (id, name, slug, parent_id) VALUES (3, 'Backend', 'backend', 2)"
)

# Get ancestor chain
hierarchy = await get_tenant_hierarchy(db, 3)
# [3, 2, 1] — Backend → Engineering → Acme Corp
```

## API Reference

### Context Functions

| Function                                                | Description                              |
| ------------------------------------------------------- | ---------------------------------------- |
| `set_tenant(tenant_id, hierarchy=None, tenant_type="")` | Set current tenant. Returns reset token. |
| `get_tenant() -> TenantRef \| None`                     | Get current tenant, or None.             |
| `clear_tenant()`                                        | Clear tenant context.                    |
| `tenant_context(tenant_id, ...)`                        | Context manager for scoped tenant.       |

### TenantRef

| Field         | Type              | Description                              |
| ------------- | ----------------- | ---------------------------------------- |
| `tenant_id`   | `int`             | Primary key of the active tenant         |
| `hierarchy`   | `tuple[int, ...]` | Ancestor chain (leaf to root)            |
| `tenant_type` | `str`             | Optional label: "org", "team", "project" |

Frozen dataclass — immutable after creation.

### TenantMiddleware

```python
TenantMiddleware(resolve_tenant=callable)
```

The `resolve_tenant` callable receives a `Request` and returns `TenantRef | None`.

### inject_tenant_condition

```python
inject_tenant_condition(model_class, conditions, params)
```

For custom admin views or raw SQL: injects `tenant_id = $N` into conditions/params lists. No-op if model doesn't use `TenantMixin` or no tenant context active.

## Configuration Table

| Setting                                    | Default  | Description                               |
| ------------------------------------------ | -------- | ----------------------------------------- |
| `TenantMixin.tenant_id`                    | Required | Integer FK to tenant table (indexed)      |
| `TenantMiddleware.resolve_tenant`          | Required | Callable to extract tenant from request   |
| `HierarchicalTenantMixin.parent_tenant_id` | `None`   | Optional parent for hierarchy             |
| `Tenant.slug`                              | Required | URL-safe identifier for subdomain routing |

## Non-Tenant Models

Models without `TenantMixin` are completely unaffected:

```python
class GlobalConfig(Model):
    class Meta:
        table = "global_config"

    key: str = Field()
    value: str = Field()


# No tenant filtering, no overhead, works exactly as before
config = await GlobalConfig.objects.filter(key="site_name").first()
```

## Security Considerations

### Resolver Trust Model

| Resolver                  | Trust Level                                       | Use Case                         |
| ------------------------- | ------------------------------------------------- | -------------------------------- |
| `resolve_from_user`       | **High** — derives tenant from authenticated user | Default for most apps            |
| `make_subdomain_resolver` | **High** — DB lookup validates slug               | Multi-domain SaaS                |
| `resolve_from_header`     | **Low** — any client can set headers              | Only behind trusted API gateway  |
| `resolve_from_url`        | **Low** — any client can craft URLs               | Only with additional auth checks |

**Never use `resolve_from_header` or `resolve_from_url` without additional authorization.** These resolvers trust the client to declare their own tenant. Always combine with authentication that verifies the user belongs to the declared tenant:

```python
def secure_header_resolver(request) -> TenantRef | None:
    """Only allow tenant switching if user belongs to that tenant."""
    ref = resolve_from_header(request)
    if ref is None:
        return None
    user = request.user
    if user is None:
        return None
    # Verify user belongs to this tenant (your app's logic)
    if not hasattr(user, "tenant_id") or user.tenant_id != ref.tenant_id:
        return None  # Deny cross-tenant access
    return ref
```

### What Tenant Filtering Does NOT Cover

- **JOINs via `select_related`**: The tenant filter is applied to the root table's WHERE clause only. FK JOINs to other tenant-aware tables do NOT automatically add `AND joined_table.tenant_id = $N`. If both tables have TenantMixin and share the same tenant_id, the FK relationship naturally scopes correctly. If they could have different tenant_ids, add explicit filters.

- **Raw SQL**: `db.query()` and `db.execute()` bypass QuerySet entirely. Use `inject_tenant_condition()` or `tenant_where_suffix()` for raw queries against tenant-aware tables.

- **SoftDeleteMixin raw operations**: `.delete()`, `.hard_delete()`, `.restore()` on individual instances use `WHERE pk = $1` without tenant filtering. This is safe when instances were loaded through the tenant-scoped QuerySet (the PK already belongs to the correct tenant).

## Migration Guide — Adding TenantMixin to Existing Models

### Step 1: Add the mixin and column

```python
# Before
class Post(Model):
    class Meta:
        table = "posts"

    title: str = Field()


# After
class Post(TenantMixin, Model):
    class Meta:
        table = "posts"

    title: str = Field()
```

### Step 2: Add the column with a default

```sql
-- Migration: add tenant_id column with a default for existing rows
ALTER TABLE posts ADD COLUMN tenant_id INTEGER NOT NULL DEFAULT 1;
CREATE INDEX idx_posts_tenant_id ON posts (tenant_id);
```

### Step 3: Backfill tenant_id from existing ownership data

```python
# Backfill script — assign rows to tenants based on your business logic
async def backfill_tenants(db):
    # Example: assign posts to tenants based on author's tenant
    await db.execute("""
        UPDATE posts SET tenant_id = (
            SELECT u.tenant_id FROM users u WHERE u.id = posts.author_id
        )
        WHERE tenant_id = 1
    """)
```

### Step 4: Remove the default

```sql
ALTER TABLE posts ALTER COLUMN tenant_id DROP DEFAULT;
```

### Step 5: Add middleware

```python
app.use(TenantMiddleware(resolve_tenant=resolve_from_user))
```

## Bulk Operations

### bulk_create

`bulk_create` does NOT go through `pre_save` signal — you must set `tenant_id` explicitly:

```python
with tenant_context(tenant_id=42):
    posts = [
        Post(title="A", tenant_id=42),  # Must set explicitly
        Post(title="B", tenant_id=42),
    ]
    await Post.objects.bulk_create(posts)
```

### QuerySet.update() and .delete()

These DO respect tenant filtering via `_build_where_tree()`:

```python
with tenant_context(tenant_id=42):
    # Only updates tenant 42's drafts
    await Post.objects.filter(status="draft").update(status="published")

    # Only deletes tenant 42's archived posts
    await Post.objects.filter(status="archived").delete()
```

### get_tenant_hierarchy

```python
async def get_tenant_hierarchy(db, tenant_id: int) -> list[int]
```

Returns ancestor chain from leaf to root using recursive CTE on `hyper_tenants` table.

## Hierarchical Tenancy

For org → team → project structures:

```python
from hyperdjango.tenancy import HierarchicalTenantMixin, Tenant, get_tenant_hierarchy


class Project(HierarchicalTenantMixin, Model):
    class Meta:
        table = "projects"

    name: str = Field()
    # Inherits: tenant_id (int), parent_tenant_id (int | None)


# Create hierarchy
# Org (id=1) → Team (id=2) → Project (id=3)
hierarchy = await get_tenant_hierarchy(db, 3)
# Returns [3, 2, 1]

# Set context with hierarchy for future cross-level queries
with tenant_context(tenant_id=3, hierarchy=tuple(hierarchy)):
    projects = await Project.objects.all()  # Scoped to tenant 3
```

`hierarchy` is stored on `TenantRef` for application code that needs to check parent/child relationships. The automatic query filter uses only `tenant_id` — hierarchical cross-level queries require explicit `.unscoped()` with manual filtering.
