"""
Multi-Tenant SaaS — Premier Tenancy Showcase.

Demonstrates every major multi-tenancy feature end-to-end:
  - TenantMixin for automatic query scoping (WHERE tenant_id = $N)
  - TenantMiddleware with header-based tenant resolution (X-Tenant-ID)
  - tenant_context() for background tasks and explicit scoping
  - .unscoped() for cross-tenant admin queries
  - Auto-set tenant_id on model save via pre_save signal
  - Tenant isolation verification (tenant A cannot see tenant B's data)
  - Per-tenant API key auth
  - Tenant CRUD (create org, manage members)
  - Nested data model: Org → Project → Task → Comment

Run:
    uv run hyper setup --app services.multi_tenant.app:app --seed services.multi_tenant.seed:run
    uv run hyper run --app services.multi_tenant.app:app --port 8920

API (all scoped by X-Tenant-ID header):
    POST /auth/login                → Login as org member
    GET  /api/projects/             → List projects (tenant-scoped)
    POST /api/projects/             → Create project (member+)
    GET  /api/projects/{id}         → Project detail
    GET  /api/tasks/                → List tasks (filterable, tenant-scoped)
    POST /api/tasks/                → Create task (member+)
    PATCH /api/tasks/{id}           → Update task (member+)
    GET  /api/tasks/{id}/comments/  → List comments on a task
    POST /api/tasks/{id}/comments/  → Add comment (any org member)
    PATCH /api/comments/{id}        → Edit own comment (author/admin)
    DELETE /api/comments/{id}       → Delete own comment (author/admin)
    GET  /api/members/              → List org members
    POST /api/members/              → Add member (admin only)
    GET  /api/stats                 → Per-tenant usage stats
    GET  /api/audit-log             → Audit trail (admin only)

    # Admin (cross-tenant, requires API key):
    GET  /api/admin/tenants                → List all tenants (unscoped)
    GET  /api/admin/stats                  → Global stats across tenants
    POST /api/admin/tenants/{id}/suspend   → Suspend a tenant
    POST /api/admin/tenants/{id}/reactivate → Reactivate a tenant
"""

import json
import time as _time
from enum import Enum
from pathlib import Path

from hyperdjango import HTTPException, HyperApp, Response
from hyperdjango.admin import HyperAdmin
from hyperdjango.auth import hash_password, verify_password
from hyperdjango.auth.api_keys import APIKeyAuth
from hyperdjango.auth.sessions import SessionAuth, build_session_data
from hyperdjango.conf import DEFAULTS, get_setting
from hyperdjango.database import get_db
from hyperdjango.guard import DenyReason, GuardDenial, Require, guard
from hyperdjango.logging import logger
from hyperdjango.mixins import TimestampMixin
from hyperdjango.models import Field, Model
from hyperdjango.openapi import mount_docs
from hyperdjango.ratelimit import RateLimitMiddleware
from hyperdjango.rest import CursorPagination
from hyperdjango.signing import SigningKey, TokenEngine
from hyperdjango.standalone_middleware import (
    CORSMiddleware,
    SecurityHeadersMiddleware,
    TimingMiddleware,
)
from hyperdjango.tenancy import (
    TenantMiddleware,
    TenantMixin,
    get_tenant,
    resolve_from_header,
    tenant_context,
)
from hyperdjango.timeline import (
    StatusTimelineMixin,
    get_timeline,
    register_timeline_admin,
)

_APP_DIR = Path(__file__).resolve().parent

# Set per-app defaults (DEFAULTS tier — env vars still override)
DEFAULTS["DATABASE_URL"] = (
    get_setting("DATABASE_URL") or "postgres://localhost/hyperdjango_test"
)

DATABASE_URL = get_setting("DATABASE_URL")

app = HyperApp(
    title="Multi-Tenant SaaS",
    database=DATABASE_URL,
    templates=str(_APP_DIR / "templates"),
    debug=get_setting("DEBUG"),
)

# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

app.use(TimingMiddleware())
app.use(SecurityHeadersMiddleware(hsts=False))
app.use(
    CORSMiddleware(
        origins=["*"],
        methods=["GET", "POST", "PATCH", "DELETE"],
        headers=["Content-Type", "X-Tenant-ID", "X-API-Key"],
    )
)
app.use(RateLimitMiddleware(max_requests=120, window=60))

_session_engine = TokenEngine(
    keys=[
        SigningKey(
            secret=get_setting("SESSION_SIGNING_KEY"),
            version=1,
        ),
    ]
)
auth = SessionAuth(
    secret=get_setting("SESSION_SECRET"),
    token_engine=_session_engine,
)
app.use(auth)
app.use(APIKeyAuth(valid_keys={get_setting("API_KEY")}))

# Tenant middleware: resolves tenant from X-Tenant-ID header
app.use(TenantMiddleware(resolve_tenant=resolve_from_header))

mount_docs(
    app,
    title="Multi-Tenant SaaS API",
    version="1.0.0",
    description="Project management SaaS with automatic tenant isolation",
)

# HyperAdmin
admin = HyperAdmin(
    app,
    prefix="/admin",
    title="Multi-Tenant Admin",
    secret_key=get_setting("ADMIN_SECRET"),
)


@app.exception_handler(Exception)
async def _handle_error(request, exc):
    logger.exception("Unhandled error: {exc}", exc=exc)
    return Response.json({"detail": "Internal server error"}, status=500)


# ---------------------------------------------------------------------------
# Models — tenant-scoped via TenantMixin
# ---------------------------------------------------------------------------


class Plan(Enum):
    FREE = "free"
    PRO = "pro"
    ENTERPRISE = "enterprise"


class Role(Enum):
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


class TaskStatus(Enum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    DONE = "done"


class Priority(Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


class ProjectStatus(Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class Org(StatusTimelineMixin, TimestampMixin):
    """Organization (tenant). Not scoped by TenantMixin since it IS the tenant.

    Lifecycle tracking via StatusTimelineMixin:
    - Default (no timeline event) = active
    - ``await org.set_status("lifecycle", "suspended", ...)`` = suspended
    - ``await org.clear_status("lifecycle", ...)`` = reactivated
    """

    class Meta:
        table = "mt_orgs"

    class TimelineConfig:
        entity_type = "org"
        categories = {"lifecycle": ["suspended"]}

    id: int = Field(primary_key=True, auto=True)
    name: str = Field()
    slug: str = Field(unique=True)
    plan: Plan = Field(default=Plan.FREE)


class Member(TenantMixin, TimestampMixin, Model):
    """Org member. tenant_id = org.id. Auto-scoped by tenant context."""

    class Meta:
        table = "mt_members"

    id: int = Field(primary_key=True, auto=True)
    username: str = Field()
    password_hash: str = Field(exclude=True)
    role: Role = Field(default=Role.MEMBER)


class Project(TenantMixin, TimestampMixin, Model):
    """Project within an org. Auto-scoped by tenant context."""

    class Meta:
        table = "mt_projects"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field()
    description: str = Field(default="")
    status: ProjectStatus = Field(default=ProjectStatus.ACTIVE)


class Task(TenantMixin, TimestampMixin, Model):
    """Task within a project. Auto-scoped by tenant context."""

    class Meta:
        table = "mt_tasks"

    id: int = Field(primary_key=True, auto=True)
    project_id: int = Field(foreign_key=Project)
    title: str = Field()
    description: str = Field(default="")
    status: TaskStatus = Field(default=TaskStatus.TODO)
    priority: Priority = Field(default=Priority.NORMAL)
    assignee: str = Field(default="")


class Comment(TenantMixin, TimestampMixin, Model):
    """Comment on a task. Auto-scoped by tenant context."""

    class Meta:
        table = "mt_comments"

    id: int = Field(primary_key=True, auto=True)
    task_id: int = Field(foreign_key=Task)
    author: str = Field()
    body: str = Field(default="")


class AuditLog(TenantMixin, TimestampMixin, Model):
    """Audit trail for write operations. Tenant-scoped."""

    class Meta:
        table = "mt_audit_log"

    id: int = Field(primary_key=True, auto=True)
    user_id: int = Field(default=0)
    username: str = Field(default="")
    action: str = Field()  # create, update, delete
    resource_type: str = Field()  # project, task, member, comment
    resource_id: int = Field(default=0)
    changes: str = Field(default="")  # JSON string of changes


async def _audit(
    tenant_id: int,
    user_id: int,
    username: str,
    action: str,
    resource_type: str,
    resource_id: int,
    changes: str = "",
) -> None:
    """Record an audit log entry via ORM."""
    entry = AuditLog(
        tenant_id=tenant_id,
        user_id=user_id,
        username=username,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        changes=changes,
    )
    await entry.save()


# ---------------------------------------------------------------------------
# Startup hook
# ---------------------------------------------------------------------------


@app.on_startup
async def _startup():
    await get_timeline().ensure_indexes()
    org_count = await Org.objects.count()
    logger.info("Multi-tenant SaaS ready: {count} orgs", count=org_count)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_tenant_cache: dict[
    int, tuple[bool, float]
] = {}  # tenant_id -> (is_active, expires_at)
_TENANT_CACHE_TTL = 60.0  # seconds


async def _verify_tenant(tenant_id: int) -> bool:
    """Verify tenant exists and is not suspended. Cached with 60s TTL.

    Active = org exists AND has no active "suspended" status in timeline.
    """
    entry = _tenant_cache.get(tenant_id)
    if entry is not None and entry[1] > _time.monotonic():
        return entry[0]
    org = await Org.objects.filter(id=tenant_id).first()
    if org is None:
        _tenant_cache[tenant_id] = (False, _time.monotonic() + _TENANT_CACHE_TTL)
        return False
    is_suspended = await org.has_status("lifecycle", "suspended")
    active = not is_suspended
    _tenant_cache[tenant_id] = (active, _time.monotonic() + _TENANT_CACHE_TTL)
    return active


async def require_tenant(request):
    """Verify tenant context is set and tenant exists/is active."""
    tenant = get_tenant()
    if tenant is None:
        raise HTTPException(400, "X-Tenant-ID header required")
    if not await _verify_tenant(tenant.tenant_id):
        raise HTTPException(403, "Tenant not found or deactivated")
    return tenant


# ---------------------------------------------------------------------------
# Guard: Role-based access control
# ---------------------------------------------------------------------------

# Role hierarchy: viewer < member < admin. Keyed on enum instance so
# ORM-hydrated member.role (a Role enum instance) looks up directly. Raw
# string lookups are handled by the helper below for compatibility.
_ROLE_LEVEL: dict[Role, int] = {
    Role.VIEWER: 0,
    Role.MEMBER: 1,
    Role.ADMIN: 2,
}


def _role_level(role: Role | str) -> int:
    """Return the numeric precedence of a role (enum or raw string)."""
    if not isinstance(role, Role):
        try:
            role = Role(role)
        except ValueError, TypeError:
            return -1
    return _ROLE_LEVEL.get(role, -1)


async def _resolve_member(request, ctx):
    """Load the authenticated user's Member record with role.

    Used as a guard resource resolver. The session stores the member ID;
    this fetches the live record to get the current role (catches
    mid-session role changes).
    """
    user = request.user
    member_id = user.id if user is not None else None
    if member_id is None:
        return None
    member = await Member.objects.filter(id=member_id).first()
    if member is None:
        return None
    return member


def _make_role_check(min_role: Role):
    """Create a guard check that requires at least the given role level."""
    min_level = _ROLE_LEVEL[min_role]

    async def _check(request, ctx):
        member = ctx.resources.get("member")
        if member is None:
            return GuardDenial(DenyReason.FORBIDDEN, "Member not found")
        if _role_level(member.role) < min_level:
            return GuardDenial(
                DenyReason.FORBIDDEN,
                f"Requires {min_role.value} role or higher",
            )
        return None

    return _check


# Reusable guard chains
REQUIRE_MEMBER = (
    Require.authenticated(),
    Require.resource("member", resolver=_resolve_member),
)
REQUIRE_WRITER = (
    *REQUIRE_MEMBER,
    Require.check("role_member", fn=_make_role_check(Role.MEMBER)),
)
REQUIRE_ADMIN = (
    *REQUIRE_MEMBER,
    Require.check("role_admin", fn=_make_role_check(Role.ADMIN)),
)


# ---------------------------------------------------------------------------
# Root redirect
# ---------------------------------------------------------------------------


@app.get("/")
async def root(request):
    return Response.redirect("/docs/")


# ---------------------------------------------------------------------------
# Routes: Auth
# ---------------------------------------------------------------------------


@app.post("/auth/login")
async def login(request):
    """Login as org member. Requires X-Tenant-ID header."""
    client_ip = request.client_ip or "unknown"
    if auth.is_login_blocked(client_ip):
        raise HTTPException(429, "Too many login attempts — please wait a few minutes")

    tenant = await require_tenant(request)
    data = await request.json()
    username = data.get("username", "")
    password = data.get("password", "")
    if not username or not password:
        raise HTTPException(400, "username and password required")

    member = await Member.objects.filter(username=username).first()
    if member is None or not verify_password(password, member.password_hash):
        auth.record_failed_login(client_ip)
        raise HTTPException(401, "Invalid credentials")

    auth.clear_login_attempts(client_ip)
    role_str = member.role.value if isinstance(member.role, Role) else member.role
    session = await build_session_data(
        member.id,
        get_db(),
        username=member.username,
        role=role_str,
        tenant_id=tenant.tenant_id,
    )
    resp = Response.json(
        {
            "message": "Logged in",
            "username": member.username,
            "role": role_str,
            "org_id": tenant.tenant_id,
        }
    )
    auth.login(resp, session)
    return resp


# ---------------------------------------------------------------------------
# Routes: Projects (tenant-scoped via TenantMixin)
# ---------------------------------------------------------------------------


@app.get("/api/projects/")
async def list_projects(request):
    """List projects for the current tenant.

    Uses CursorPagination (keyset, HMAC-signed, no OFFSET scanning).
    TenantQuerySet auto-injects WHERE tenant_id = $N.
    """
    await require_tenant(request)
    paginator = CursorPagination()
    paginator.page_size = 50
    paginator.ordering = "-id"

    qs = Project.objects
    items = await paginator.paginate_queryset(qs, request)
    data = [
        {
            "id": p.id,
            "name": p.name,
            "description": p.description,
            "status": p.status,
            "created_at": str(p.created_at),
        }
        for p in items
    ]
    return paginator.get_paginated_response(data)


@app.post("/api/projects/")
@guard(*REQUIRE_WRITER)
async def create_project(request):
    """Create a project in the current tenant. Requires member role or higher."""
    tenant = await require_tenant(request)
    data = await request.json()
    name = data.get("name", "").strip()
    if not name:
        raise HTTPException(400, "name required")

    project = Project(
        name=name,
        description=data.get("description", ""),
        status="active",
        tenant_id=tenant.tenant_id,
    )
    await project.save()
    await _audit(
        tenant.tenant_id,
        request.user["id"],
        request.user["username"],
        "create",
        "project",
        project.id,
        json.dumps({"name": name}),
    )
    return Response.json(
        project.to_dict(include={"id", "name", "tenant_id", "status"}), status=201
    )


@app.get("/api/projects/{id:int}")
async def get_project(request, id):
    """Get project detail (tenant-scoped)."""
    await require_tenant(request)
    project = await Project.objects.filter(id=id).first()
    if project is None:
        raise HTTPException(404, "Project not found")

    # Count tasks in this project (also tenant-scoped)
    task_count = await Task.objects.filter(project_id=id).count()

    return Response.json(
        {
            "id": project.id,
            "name": project.name,
            "description": project.description,
            "status": project.status,
            "task_count": task_count,
            "created_at": str(project.created_at),
        }
    )


# ---------------------------------------------------------------------------
# Routes: Tasks (tenant-scoped)
# ---------------------------------------------------------------------------


@app.get("/api/tasks/")
async def list_tasks(request):
    """List tasks for the current tenant (filterable, cursor-paginated)."""
    await require_tenant(request)
    project_id = request.query("project_id", "")
    status_filter = request.query("status", "")

    qs = Task.objects
    if project_id:
        qs = qs.filter(project_id=int(project_id))
    if status_filter:
        qs = qs.filter(status=status_filter)

    paginator = CursorPagination()
    paginator.page_size = 50
    paginator.ordering = "-id"

    items = await paginator.paginate_queryset(qs, request)
    data = [
        {
            "id": t.id,
            "project_id": t.project_id,
            "title": t.title,
            "status": t.status,
            "priority": t.priority,
            "assignee": t.assignee,
            "created_at": str(t.created_at),
        }
        for t in items
    ]
    return paginator.get_paginated_response(data)


@app.post("/api/tasks/")
@guard(*REQUIRE_WRITER)
async def create_task(request):
    """Create a task in a project (tenant-scoped). Requires member role or higher."""
    tenant = await require_tenant(request)
    data = await request.json()
    project_id = data.get("project_id")
    title = data.get("title", "").strip()

    if not project_id or not title:
        raise HTTPException(400, "project_id and title required")
    priority = data.get("priority", "normal")
    try:
        Priority(priority)
    except ValueError:
        raise HTTPException(
            400,
            f"Invalid priority. Must be one of: {', '.join(p.value for p in Priority)}",
        )

    # Verify project belongs to this tenant
    project = await Project.objects.filter(id=project_id).first()
    if project is None:
        raise HTTPException(404, "Project not found")

    task = Task(
        project_id=project_id,
        title=title,
        description=data.get("description", ""),
        status="todo",
        priority=data.get("priority", "normal"),
        assignee=data.get("assignee", ""),
        tenant_id=tenant.tenant_id,
    )
    await task.save()
    await _audit(
        tenant.tenant_id,
        request.user["id"],
        request.user["username"],
        "create",
        "task",
        task.id,
        json.dumps({"title": title, "project_id": project_id}),
    )
    return Response.json(
        task.to_dict(include={"id", "project_id", "title", "status", "tenant_id"}),
        status=201,
    )


@app.patch("/api/tasks/{id:int}")
@guard(*REQUIRE_WRITER)
async def update_task(request, id):
    """Update task status/assignee (tenant-scoped). Requires member role or higher."""
    tenant = await require_tenant(request)
    task = await Task.objects.filter(id=id, tenant_id=tenant.tenant_id).first()
    if task is None:
        raise HTTPException(404, "Task not found")

    data = await request.json()
    _UPDATABLE = frozenset(("status", "priority", "assignee", "title", "description"))
    changed = {}
    for key in _UPDATABLE:
        if key not in data:
            continue
        val = str(data[key])
        if key == "status":
            try:
                TaskStatus(val)
            except ValueError:
                raise HTTPException(
                    400,
                    f"Invalid status. Must be one of: {', '.join(s.value for s in TaskStatus)}",
                )
        if key == "priority":
            try:
                Priority(val)
            except ValueError:
                raise HTTPException(
                    400,
                    f"Invalid priority. Must be one of: {', '.join(p.value for p in Priority)}",
                )
        changed[key] = val

    if not changed:
        raise HTTPException(400, "No valid fields to update")

    await Task.objects.filter(id=id, tenant_id=tenant.tenant_id).update(**changed)
    task = await Task.objects.filter(id=id, tenant_id=tenant.tenant_id).first()
    await _audit(
        tenant.tenant_id,
        request.user["id"],
        request.user["username"],
        "update",
        "task",
        id,
        json.dumps(changed),
    )
    return Response.json(
        {
            "id": task.id,
            "title": task.title,
            "status": task.status,
            "priority": task.priority,
            "assignee": task.assignee,
        }
    )


# ---------------------------------------------------------------------------
# Routes: Members (tenant-scoped)
# ---------------------------------------------------------------------------


@app.get("/api/members/")
async def list_members(request):
    """List members of the current org (cursor-paginated)."""
    await require_tenant(request)
    paginator = CursorPagination()
    paginator.page_size = 50
    paginator.ordering = "id"

    items = await paginator.paginate_queryset(Member.objects, request)
    data = [
        {
            "id": m.id,
            "username": m.username,
            "role": m.role,
            "created_at": str(m.created_at),
        }
        for m in items
    ]
    return paginator.get_paginated_response(data)


@app.post("/api/members/")
@guard(*REQUIRE_ADMIN)
async def add_member(request):
    """Add a member to the current org. Requires admin role."""
    tenant = await require_tenant(request)
    data = await request.json()
    username = data.get("username", "").strip()
    password = data.get("password", "")
    role = data.get("role", "member")

    if not username or len(password) < 8:
        raise HTTPException(400, "username required, password min 8 chars")
    try:
        Role(role)
    except ValueError:
        raise HTTPException(
            400, f"Invalid role. Must be one of: {', '.join(r.value for r in Role)}"
        )

    # Check uniqueness within tenant (auto-scoped by TenantMixin)
    existing = await Member.objects.filter(username=username).first()
    if existing is not None:
        raise HTTPException(409, "Username already exists in this org")

    member = Member(
        username=username,
        password_hash=hash_password(password),
        role=role,
        tenant_id=tenant.tenant_id,
    )
    await member.save()
    await _audit(
        tenant.tenant_id,
        request.user["id"],
        request.user["username"],
        "create",
        "member",
        member.id,
        json.dumps({"username": username, "role": role}),
    )
    return Response.json(
        {
            "id": member.id,
            "username": member.username,
            "role": member.role,
            "tenant_id": tenant.tenant_id,
        },
        status=201,
    )


# ---------------------------------------------------------------------------
# Routes: Stats (tenant-scoped)
# ---------------------------------------------------------------------------


@app.get("/api/stats")
async def tenant_stats(request):
    """Per-tenant usage statistics (single query)."""
    tenant = await require_tenant(request)
    tid = tenant.tenant_id

    # ORM counts — auto-scoped by tenant context
    projects = await Project.objects.count()
    tasks = await Task.objects.count()
    tasks_done = await Task.objects.filter(status="done").count()
    members = await Member.objects.count()

    return Response.json(
        {
            "org_id": tid,
            "projects": projects,
            "tasks": tasks,
            "tasks_done": tasks_done,
            "members": members,
        }
    )


# ---------------------------------------------------------------------------
# Routes: Comments (tenant-scoped, nested under tasks)
# ---------------------------------------------------------------------------


@app.get("/api/tasks/{id:int}/comments/")
async def list_comments(request, id):
    """List comments for a task (tenant-scoped, cursor-paginated)."""
    await require_tenant(request)
    task_id = id
    # Existence check only — no task fields are read downstream, so
    # `.exists()` (SELECT 1 ... LIMIT 1) is cheaper than fetching the
    # full row via `.filter(...).first()`.
    if not await Task.objects.filter(id=task_id).exists():
        raise HTTPException(404, "Task not found")

    paginator = CursorPagination()
    paginator.page_size = 50
    paginator.ordering = "-id"

    qs = Comment.objects.filter(task_id=task_id)
    items = await paginator.paginate_queryset(qs, request)
    data = [
        {
            "id": c.id,
            "task_id": c.task_id,
            "author": c.author,
            "body": c.body,
            "created_at": str(c.created_at),
        }
        for c in items
    ]
    return paginator.get_paginated_response(data)


@app.post("/api/tasks/{id:int}/comments/")
@guard(*REQUIRE_MEMBER)
async def create_comment(request, id):
    """Add a comment to a task. Any authenticated member can comment."""
    tenant = await require_tenant(request)
    task_id = id
    # Existence check only — same rationale as list_comments above.
    if not await Task.objects.filter(id=task_id).exists():
        raise HTTPException(404, "Task not found")

    data = await request.json()
    body = data.get("body", "").strip()
    if not body:
        raise HTTPException(400, "body required")

    member = request.guard.member
    comment = Comment(
        task_id=task_id,
        author=member.username,
        body=body,
        tenant_id=tenant.tenant_id,
    )
    await comment.save()
    await _audit(
        tenant.tenant_id,
        request.user["id"],
        request.user["username"],
        "create",
        "comment",
        comment.id,
        json.dumps({"task_id": task_id}),
    )
    return Response.json(
        comment.to_dict(include={"id", "task_id", "author", "body", "created_at"}),
        status=201,
    )


@app.patch("/api/comments/{id:int}")
@guard(*REQUIRE_MEMBER)
async def update_comment(request, id):
    """Update own comment (author or admin only)."""
    tenant = await require_tenant(request)
    comment = await Comment.objects.filter(id=id).first()
    if comment is None:
        raise HTTPException(404, "Comment not found")

    member = request.guard.member
    # Only author or admin can edit
    if comment.author != member.username and member.role != Role.ADMIN.value:
        raise HTTPException(403, "You can only edit your own comments")

    data = await request.json()
    body = data.get("body", "").strip()
    if not body:
        raise HTTPException(400, "body required")

    comment.body = body
    await comment.save()
    await _audit(
        tenant.tenant_id,
        request.user["id"],
        request.user["username"],
        "update",
        "comment",
        id,
        json.dumps({"body": body}),
    )
    return Response.json(comment.to_dict(include={"id", "task_id", "author", "body"}))


@app.delete("/api/comments/{id:int}")
@guard(*REQUIRE_MEMBER)
async def delete_comment(request, id):
    """Delete own comment (author or admin only)."""
    tenant = await require_tenant(request)
    comment = await Comment.objects.filter(id=id).first()
    if comment is None:
        raise HTTPException(404, "Comment not found")

    member = request.guard.member
    if comment.author != member.username and member.role != Role.ADMIN.value:
        raise HTTPException(403, "You can only delete your own comments")

    await Comment.objects.filter(id=id).delete()
    await _audit(
        tenant.tenant_id,
        request.user["id"],
        request.user["username"],
        "delete",
        "comment",
        id,
    )
    return Response.json({"ok": True})


# ---------------------------------------------------------------------------
# Routes: Audit Log (tenant-scoped, admin only)
# ---------------------------------------------------------------------------


@app.get("/api/audit-log")
@guard(*REQUIRE_ADMIN)
async def audit_log(request):
    """View audit log for the current tenant. Admin only, cursor-paginated."""
    tenant = await require_tenant(request)
    paginator = CursorPagination()
    paginator.page_size = 50
    paginator.ordering = "-id"

    qs = AuditLog.objects  # TenantMixin auto-scopes to current tenant
    items = await paginator.paginate_queryset(qs, request)
    data = [
        {
            "id": a.id,
            "user_id": a.user_id,
            "username": a.username,
            "action": a.action,
            "resource_type": a.resource_type,
            "resource_id": a.resource_id,
            "changes": a.changes,
            "created_at": str(a.created_at),
        }
        for a in items
    ]
    return paginator.get_paginated_response(data)


# ---------------------------------------------------------------------------
# Routes: Admin (cross-tenant, API key protected)
# ---------------------------------------------------------------------------


@app.get("/api/admin/tenants")
@guard(Require.api_key())
async def admin_list_tenants(request):
    """List all tenants (unscoped, admin only).

    Derives ``is_active`` from timeline — org is active unless it has
    a current "suspended" lifecycle status.
    """
    orgs = await Org.objects.order_by("id").all()
    tl = get_timeline()
    result: list[dict[str, str | int | bool]] = []
    for o in orgs:
        is_suspended = await tl.is_active("org", o.id, "suspended")
        result.append(
            {
                "id": o.id,
                "name": o.name,
                "slug": o.slug,
                "plan": o.plan,
                "is_active": not is_suspended,
                "created_at": str(o.created_at),
            }
        )
    return Response.json(result)


@app.get("/api/admin/stats")
@guard(Require.api_key())
async def admin_global_stats(request):
    """Global stats across all tenants (single query)."""
    # Cross-tenant stats — use unscoped() to bypass tenant filtering
    total_orgs = await Org.objects.count()
    total_projects = await Project.objects.unscoped().count()
    total_tasks = await Task.objects.unscoped().count()
    total_members = await Member.objects.unscoped().count()
    return Response.json(
        {
            "total_orgs": total_orgs,
            "total_projects": total_projects,
            "total_tasks": total_tasks,
            "total_members": total_members,
        }
    )


# ---------------------------------------------------------------------------
# Routes: Tenant context demo
# ---------------------------------------------------------------------------


@app.post("/api/admin/tenants/{id:int}/suspend")
@guard(Require.api_key())
async def admin_suspend_tenant(request, id):
    """Suspend a tenant via timeline lifecycle event. Admin API key required."""
    org = await Org.objects.filter(id=id).first()
    if org is None:
        raise HTTPException(404, "Tenant not found")
    await org.set_status("lifecycle", "suspended", reason="Admin suspension")
    _tenant_cache.clear()
    await _audit(id, 0, "api_admin", "suspend", "tenant", id)
    return Response.json({"id": org.id, "name": org.name, "is_active": False})


@app.post("/api/admin/tenants/{id:int}/reactivate")
@guard(Require.api_key())
async def admin_reactivate_tenant(request, id):
    """Reactivate a suspended tenant by ending the lifecycle event. Admin API key required."""
    org = await Org.objects.filter(id=id).first()
    if org is None:
        raise HTTPException(404, "Tenant not found")
    await org.clear_status("lifecycle", reason="Admin reactivation")
    _tenant_cache.clear()
    await _audit(id, 0, "api_admin", "reactivate", "tenant", id)
    return Response.json({"id": org.id, "name": org.name, "is_active": True})


@app.get("/api/cross-tenant-demo")
@guard(Require.api_key())
async def cross_tenant_demo(request):
    """Demonstrate unscoped() and tenant_context() — admin only.

    Shows that:
    1. .unscoped() returns ALL projects across tenants
    2. tenant_context(N) scopes to tenant N explicitly
    """
    # 1. Unscoped query — all projects across all tenants
    all_projects = await Project.objects.unscoped().all()

    # 2. Explicit tenant_context — scope to tenant 1
    with tenant_context(tenant_id=1):
        t1_projects = await Project.objects.all()

    # 3. Explicit tenant_context — scope to tenant 2
    with tenant_context(tenant_id=2):
        t2_projects = await Project.objects.all()

    return Response.json(
        {
            "unscoped_total": len(all_projects),
            "tenant_1_projects": len(t1_projects),
            "tenant_2_projects": len(t2_projects),
            "isolation_verified": len(t1_projects) + len(t2_projects)
            <= len(all_projects),
        }
    )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


app.mount_health()


# ---------------------------------------------------------------------------
# HyperAdmin model registration
# ---------------------------------------------------------------------------

admin.register(
    Org,
    list_display=["id", "name", "slug", "plan"],
    search_fields=["name", "slug"],
    list_filter=["plan"],
)

admin.register(
    Member,
    list_display=["id", "username", "role", "tenant_id", "created_at"],
    search_fields=["username"],
    list_filter=["role"],
)

admin.register(
    Project,
    list_display=["id", "name", "status", "tenant_id", "created_at"],
    search_fields=["name"],
    list_filter=["status"],
)

admin.register(
    Task,
    list_display=["id", "title", "status", "priority", "assignee", "project_id"],
    search_fields=["title"],
    list_filter=["status", "priority"],
    ordering="-created_at",
)

admin.register(
    Comment,
    list_display=["id", "task_id", "author", "created_at"],
    search_fields=["body", "author"],
    ordering="-created_at",
)

admin.register(
    AuditLog,
    list_display=[
        "id",
        "username",
        "action",
        "resource_type",
        "resource_id",
        "created_at",
    ],
    search_fields=["username", "action"],
    list_filter=["action", "resource_type"],
    ordering="-created_at",
)

register_timeline_admin(admin)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8920)
