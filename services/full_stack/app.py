"""
Full-Stack Task Manager — Complete Reference Scaffold.

The "start here" app for new HyperDjango developers. Demonstrates:
  - Session auth (register/login/logout)
  - 3 models with FK relationships (User, Project, Task)
  - Template inheritance (base.html → child pages)
  - Form handling with validation
  - Flash messages across redirects
  - HyperAdmin panel
  - Health/readiness probes
  - Error pages

Setup:
    uv run hyper setup --app services.full_stack.app:app --seed services.full_stack.seed:run
    uv run hyper run --app services.full_stack.app:app --port 8400
"""

import sys
from enum import Enum
from pathlib import Path

from hyperdjango import BaseModel as ValidatedModel
from hyperdjango import HTTPException, HyperApp, Response
from hyperdjango.admin import HyperAdmin
from hyperdjango.auth.permissions import PermissionChecker
from hyperdjango.auth.sessions import SessionAuth, build_session_data
from hyperdjango.auth.user import User
from hyperdjango.conf import DEFAULTS, get_setting
from hyperdjango.database import get_db
from hyperdjango.logging import logger
from hyperdjango.mixins import TimestampMixin
from hyperdjango.models import Field, Model
from hyperdjango.openapi import mount_docs
from hyperdjango.signing import SigningKey, TokenEngine
from hyperdjango.standalone_middleware import (
    CSRFMiddleware,
    SecurityHeadersMiddleware,
    TimingMiddleware,
    VersionMiddleware,
)
from hyperdjango.validation.core.fields import Field as VField
from hyperdjango.validation.core.validator import ValidationErrors

_APP_DIR = Path(__file__).resolve().parent

# Set per-app defaults (DEFAULTS tier — env vars still override)
DEFAULTS["DATABASE_URL"] = (
    get_setting("DATABASE_URL") or "postgres://localhost/hyperdjango_test"
)

DATABASE_URL = get_setting("DATABASE_URL")

app = HyperApp(
    title="Task Manager",
    database=DATABASE_URL,
    templates=str(_APP_DIR / "templates"),
    debug=get_setting("DEBUG"),
)

# Middleware
app.use(VersionMiddleware())
app.use(TimingMiddleware())
app.use(SecurityHeadersMiddleware(hsts=False))
app.use(
    CSRFMiddleware(
        secret=get_setting("CSRF_SECRET"),
        exempt_prefixes={"/admin/"},
    )
)

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

# HyperAdmin
admin = HyperAdmin(
    app,
    prefix="/admin",
    title="Task Manager Admin",
    secret_key=get_setting("ADMIN_SECRET"),
)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


@app.on_startup
async def _startup():
    db = get_db()
    checker = PermissionChecker(db)
    await checker.ensure_tables()


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TaskStatus(Enum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    DONE = "done"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


# User model: uses framework hyper_users table + RBAC groups.
# Imported from hyperdjango.auth.user as `User` above.


class Project(TimestampMixin, Model):
    class Meta:
        table = "fs_projects"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field()
    description: str = Field(default="")
    owner_id: int = Field(foreign_key=User)


class Task(TimestampMixin, Model):
    class Meta:
        table = "fs_tasks"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field()
    description: str = Field(default="")
    status: TaskStatus = Field(default=TaskStatus.TODO)
    project_id: int = Field(foreign_key=Project)
    assignee_id: int = Field(default=0)


# ---------------------------------------------------------------------------
# Validation schemas
# ---------------------------------------------------------------------------

_VALID_STATUSES = frozenset(s.value for s in TaskStatus)


class LoginSchema(ValidatedModel):
    username: str = VField(min_length=1, strip_whitespace=True)
    password: str = VField(min_length=1)


class RegisterSchema(ValidatedModel):
    username: str = VField(
        min_length=1, max_length=50, pattern=r"^[a-zA-Z0-9_-]+$", strip_whitespace=True
    )
    email: str = VField(default="", max_length=254, strip_whitespace=True)
    password: str = VField(min_length=8)


class ProjectSchema(ValidatedModel):
    name: str = VField(min_length=1, max_length=200, strip_whitespace=True)
    description: str = VField(default="", max_length=2000, strip_whitespace=True)


class TaskSchema(ValidatedModel):
    title: str = VField(min_length=1, max_length=200, strip_whitespace=True)


class TaskStatusSchema(ValidatedModel):
    status: str = VField(min_length=1)


async def _validate_form(request, schema_cls):
    """Parse form data and validate with schema. Returns validated model."""
    raw = await request.form()
    flat: dict[str, str] = {}
    for key, val in raw.items():
        if key == "_csrf_token":
            continue
        if isinstance(val, list):
            flat[key] = val[0] if val else ""
        else:
            flat[key] = val
    return schema_cls.model_validate_strings(flat)


def _render(request, template: str, ctx: dict | None = None, status: int = 200):
    """Render a template with CSRF token injected from the request cookie."""
    context = dict(ctx or {})
    context["csrf_token"] = request.cookies.get("csrftoken", "")
    return app.render(template, context, status=status)


# ---------------------------------------------------------------------------
# Exception handlers
# ---------------------------------------------------------------------------


@app.exception_handler(Exception)
async def _handle_generic(request, exc):
    logger.exception("Unhandled error: {err}", err=str(exc))
    # Platform convention: `{"detail": "..."}` only — HTTP status code
    # is canonical on the wire.
    return Response.json({"detail": "Internal server error"}, status=500)


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def _get_user_id(request) -> int | None:
    if request.user is not None and request.user.is_authenticated:
        return request.user.id
    return None


async def _get_current_user(request) -> User | None:
    uid = _get_user_id(request)
    if uid is None:
        return None
    return await User.objects.filter(id=uid).first()


def _require_auth(request) -> int:
    uid = _get_user_id(request)
    if uid is None:
        raise HTTPException(401, "Login required")
    return uid


# ---------------------------------------------------------------------------
# Routes: Auth
# ---------------------------------------------------------------------------


@app.get("/login")
async def login_page(request):
    return _render(request, "login.html", {"error": ""})


@app.post("/login")
async def login_submit(request):
    client_ip = request.client_ip or "unknown"
    if auth.is_login_blocked(client_ip):
        return _render(
            request, "login.html", {"error": "Too many attempts — please wait"}
        )
    try:
        data = await _validate_form(request, LoginSchema)
    except ValidationErrors as exc:
        return _render(request, "login.html", {"error": str(exc)})
    db = get_db()
    checker = PermissionChecker(db)
    user_dict = await checker.authenticate(data.username, data.password)
    if user_dict is None:
        auth.record_failed_login(client_ip)
        return _render(request, "login.html", {"error": "Invalid credentials"})
    auth.clear_login_attempts(client_ip)
    resp = Response.redirect("/")
    session = await build_session_data(
        user_dict["id"], db, username=user_dict["username"]
    )
    auth.login(resp, session, request)
    return resp


@app.get("/register")
async def register_page(request):
    return _render(request, "register.html", {"error": ""})


@app.post("/register")
async def register_submit(request):
    try:
        data = await _validate_form(request, RegisterSchema)
    except ValidationErrors as exc:
        return _render(request, "register.html", {"error": str(exc)})
    existing = await User.objects.filter(username=data.username).first()
    if existing:
        return _render(request, "register.html", {"error": "Username taken"})
    db = get_db()
    checker = PermissionChecker(db)
    user = await checker.create_user(data.username, data.password, email=data.email)
    resp = Response.redirect("/")
    session = await build_session_data(user.id, db, username=data.username)
    auth.login(resp, session, request)
    return resp


@app.post("/logout")
async def logout_post(request):
    resp = Response.redirect("/login")
    if request.session_id:
        auth.logout(resp, request.session_id)
    return resp


# ---------------------------------------------------------------------------
# Routes: Dashboard
# ---------------------------------------------------------------------------


@app.get("/")
async def dashboard(request):
    user = await _get_current_user(request)
    if not user:
        return Response.redirect("/login")
    projects = await Project.objects.filter(owner_id=user.id).order_by("-id").all()
    task_count = await Task.objects.count()
    return _render(
        request,
        "dashboard.html",
        {
            "user": user,
            "projects": projects,
            "task_count": task_count,
        },
    )


# ---------------------------------------------------------------------------
# Routes: Projects
# ---------------------------------------------------------------------------


@app.get("/projects/new")
async def project_new(request):
    user = await _get_current_user(request)
    if not user:
        return Response.redirect("/login")
    return _render(request, "project_form.html", {"user": user, "error": ""})


@app.post("/projects/new")
async def project_create(request):
    uid = _require_auth(request)
    try:
        data = await _validate_form(request, ProjectSchema)
    except ValidationErrors as exc:
        user = await User.objects.filter(id=uid).first()
        return _render(request, "project_form.html", {"user": user, "error": str(exc)})
    project = Project(name=data.name, description=data.description, owner_id=uid)
    await project.save()
    return Response.redirect(f"/projects/{project.id}")


@app.get("/projects/{id:int}")
async def project_detail(request, id: int):
    user = await _get_current_user(request)
    if not user:
        return Response.redirect("/login")
    project = await Project.objects.filter(id=id, owner_id=user.id).first()
    if not project:
        raise HTTPException(404, "Project not found")
    tasks = await Task.objects.filter(project_id=id).order_by("-id").all()
    return _render(
        request,
        "project_detail.html",
        {
            "user": user,
            "project": project,
            "tasks": tasks,
            "statuses": [s.value for s in TaskStatus],
        },
    )


# ---------------------------------------------------------------------------
# Routes: Tasks
# ---------------------------------------------------------------------------


@app.post("/projects/{id:int}/tasks")
async def task_create(request, id: int):
    uid = _require_auth(request)
    project = await Project.objects.filter(id=id, owner_id=uid).first()
    if not project:
        raise HTTPException(404, "Project not found")
    try:
        data = await _validate_form(request, TaskSchema)
    except ValidationErrors:
        return Response.redirect(f"/projects/{id}")
    task = Task(title=data.title, project_id=id)
    await task.save()
    return Response.redirect(f"/projects/{id}")


async def _get_owned_task(task_id: int, user_id: int) -> Task:
    """Fetch a task and verify the user owns the parent project."""
    task = await Task.objects.filter(id=task_id).first()
    if not task:
        raise HTTPException(404, "Task not found")
    project = await Project.objects.filter(id=task.project_id, owner_id=user_id).first()
    if not project:
        raise HTTPException(404, "Task not found")
    return task


@app.post("/tasks/{id:int}/status")
async def task_update_status(request, id: int):
    uid = _require_auth(request)
    task = await _get_owned_task(id, uid)
    try:
        data = await _validate_form(request, TaskStatusSchema)
    except ValidationErrors:
        return Response.redirect(f"/projects/{task.project_id}")
    if data.status not in _VALID_STATUSES:
        raise HTTPException(
            400, f"Invalid status. Must be one of: {', '.join(sorted(_VALID_STATUSES))}"
        )
    await Task.objects.filter(id=id).update(status=data.status)
    return Response.redirect(f"/projects/{task.project_id}")


@app.post("/tasks/{id:int}/delete")
async def task_delete(request, id: int):
    uid = _require_auth(request)
    task = await _get_owned_task(id, uid)
    project_id = task.project_id
    await Task.objects.filter(id=id).delete()
    return Response.redirect(f"/projects/{project_id}")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@app.get("/api/projects")
async def api_projects(request):
    uid = _require_auth(request)
    projects = await Project.objects.filter(owner_id=uid).order_by("-id").all()
    return Response.json(
        {
            "projects": [
                {"id": p.id, "name": p.name, "description": p.description}
                for p in projects
            ]
        }
    )


@app.get("/api/projects/{id:int}/tasks")
async def api_project_tasks(request, id: int):
    uid = _require_auth(request)
    project = await Project.objects.filter(id=id, owner_id=uid).first()
    if not project:
        raise HTTPException(404, "Project not found")
    tasks = await Task.objects.filter(project_id=id).order_by("-id").all()
    # t.status is always a TaskStatus enum — the field is annotated
    # `status: TaskStatus` and the from_record enum coercer enforces
    # it on load. No defensive isinstance guard needed.
    return Response.json(
        {
            "tasks": [
                {
                    "id": t.id,
                    "title": t.title,
                    "status": t.status.value,
                }
                for t in tasks
            ]
        }
    )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


app.mount_health()
app.mount_version()
mount_docs(app)


# ---------------------------------------------------------------------------
# HyperAdmin registration
# ---------------------------------------------------------------------------

# User management is handled by HyperAdmin's built-in User/Group/Permission CRUD.

admin.register(
    Project,
    list_display=["id", "name", "owner_id", "created_at"],
    search_fields=["name"],
)

admin.register(
    Task,
    list_display=["id", "title", "status", "project_id", "created_at"],
    search_fields=["title"],
    list_filter=["status"],
    ordering="-created_at",
)


if __name__ == "__main__":
    _port = int(sys.argv[1]) if len(sys.argv) > 1 else get_setting("PORT", 8400)
    app.run(port=_port)
