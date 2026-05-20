# Multi-Tenant SaaS

Project management SaaS demonstrating automatic tenant isolation with TenantMixin, header-based tenant resolution, and cross-tenant admin queries.

## Quick Start

```bash
uv run hyper setup --app services.multi_tenant.app:app --seed services.multi_tenant.seed:run
uv run hyper run --app services.multi_tenant.app:app --port 8920

# All API calls require X-Tenant-ID header:
curl -H "X-Tenant-ID: 1" http://localhost:8920/api/projects/
```

## Features

- TenantMixin automatically injects `WHERE tenant_id = $N` on all queries
- TenantMiddleware resolves tenant from `X-Tenant-ID` header
- `tenant_context()` for explicit scoping in background tasks
- `.unscoped()` for cross-tenant admin queries
- Nested data model: Org -> Project -> Task -> Comment (all tenant-scoped)
- Per-tenant member management with role-based access (admin/member/viewer)
- Org plans (free/pro/enterprise) with tenant verification cache
- Cross-tenant admin endpoints protected by API key
- Tenant isolation demo endpoint proving data separation
- OpenAPI docs at `/docs`

## Platform Features Demonstrated

- **TenantMixin** for automatic query scoping on Model subclasses
- **TenantMiddleware** with `resolve_from_header` tenant resolver
- **tenant_context()** context manager for explicit tenant scoping
- **.unscoped()** to bypass tenant filtering for admin operations
- **CursorPagination** on all list endpoints
- **APIKeyAuth** for cross-tenant admin access
- **Enum fields** for Plan, Role, TaskStatus, Priority, ProjectStatus
- **mount_docs()** for OpenAPI generation

## API Endpoints

All tenant-scoped endpoints require `X-Tenant-ID` header:

```
POST /auth/login                        Login as org member
GET  /api/projects/                     List projects (tenant-scoped)
POST /api/projects/                     Create project (auth required)
GET  /api/projects/{id}                 Project detail with task count
GET  /api/tasks/                        List tasks (filterable by project/status)
POST /api/tasks/                        Create task
PATCH /api/tasks/{id}                   Update task status/assignee
GET  /api/members/                      List org members
POST /api/members/                      Add member to org
GET  /api/stats                         Per-tenant usage statistics

# Admin (cross-tenant, API key required):
GET  /api/admin/tenants                 List all tenants (unscoped)
GET  /api/admin/stats                   Global stats across tenants
GET  /api/cross-tenant-demo             Demonstrates unscoped() and tenant_context()
```

## HyperAdmin Panel

Admin panel at `/admin/` with all 6 models registered:

- Org (search by name/slug, filter by plan)
- Member (search by username, filter by role)
- Project (search by name, filter by status)
- Task (search by title, filter by status/priority)
- Comment (search by body/author)
- AuditLog (search by username/action, filter by action/resource_type)

## Project Structure

```
multi_tenant/
    app.py          Tenant-scoped models, TenantMiddleware, CRUD routes, admin
    seed.py         Sample orgs, members, projects, tasks
    templates/      (reserved for future HTML views)
```
