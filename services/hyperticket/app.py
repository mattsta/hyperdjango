"""
HyperTicket — Production-Grade SaaS Ticketing System.

Premier showcase of every major HyperDjango platform feature:
  - Multi-tenancy (TenantMixin, TenantMiddleware, tenant isolation)
  - Guard-based access control (intent resolvers, role hierarchy)
  - BaseModel input validation (VField constraints on every input)
  - Session auth (agent + customer dual auth flows)
  - HyperAdmin (all 27 models with fieldsets, actions, filters)
  - HTMX-powered interactive UI (ticket list, detail, comments)
  - ORM-based data access (no raw SQL for CRUD)
  - Background tasks (@app.task, cron scheduling)
  - Realtime (WebSocket, LiveQuery, NotificationManager)
  - Metering/quotas (MeterEngine, per-plan enforcement)

Run:
    uv run hyper setup --app services.hyperticket.app:app --seed services.hyperticket.seed:run
    uv run hyper start --app services.hyperticket.app:app --port 8930

Portals:
    Agent:    /tickets/, /agents/, /teams/
    Customer: /portal/tickets/
    Admin:    /admin/
    Auth:     /auth/agent/login, /auth/customer/register
    Health:   /health, /ready
"""

import dataclasses
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

from hyperdjango import BaseModel as ValidatedModel
from hyperdjango import HTTPException, HyperApp, Response
from hyperdjango.admin import HyperAdmin
from hyperdjango.admin.fields import Action
from hyperdjango.auth import hash_password, verify_password
from hyperdjango.auth.sessions import SessionAuth, build_session_data
from hyperdjango.conf import DEFAULTS, get_setting
from hyperdjango.database import get_db
from hyperdjango.expressions import Count
from hyperdjango.guard import Require, guard
from hyperdjango.guard.types import DenyReason, GuardDenial
from hyperdjango.humanize import time_bucket_cached
from hyperdjango.logging import logger
from hyperdjango.ratelimit import RateLimitMiddleware
from hyperdjango.rest import CursorPagination
from hyperdjango.signing import SigningKey, TokenEngine
from hyperdjango.standalone_middleware import (
    CORSMiddleware,
    CSRFMiddleware,
    SecurityHeadersMiddleware,
    TimingMiddleware,
)
from hyperdjango.storage import MemoryStorage
from hyperdjango.telemetry import configure_from_settings
from hyperdjango.tenancy import TenantMiddleware, TenantRef, get_tenant
from hyperdjango.timeline import get_timeline, register_timeline_admin
from hyperdjango.validation.core.fields import Field as VField
from hyperdjango.validation.core.validator import ValidationErrors

from . import signals as _signals  # noqa: F401 — connects post_save handlers
from .adapters import adapter_registry
from .adapters.ai_moderation import AIContentModerationAdapter
from .adapters.ai_triage import AITriageAdapter
from .adapters.protocols import AdapterContext
from .config import load_hyperticket_config
from .models import (
    ActivityAction,
    ActivityLog,
    Agent,
    AgentRole,
    AgentSkill,
    Approval,
    ApprovalStatus,
    Attachment,
    AuthorType,
    CannedResponse,
    Comment,
    Customer,
    EscalationRule,
    NotificationPreference,
    Org,
    OrgAPIKey,
    OrgSettings,
    PlanConfig,
    PlanFeatureLimit,
    PriorityConfig,
    RelationType,
    SatisfactionRating,
    SavedView,
    SLAInstance,
    SLAPolicy,
    StatusTransition,
    Tag,
    Team,
    TeamMembership,
    TenantTheme,
    Ticket,
    TicketRelation,
    TicketSource,
    TicketStatusConfig,
    TicketTag,
    TicketTemplate,
    TicketTypeConfig,
    WorkflowRule,
)
from .realtime import broadcast_notification, register_ws_endpoint
from .realtime import (
    handlers as _rt_handlers,  # noqa: F401 — connects broadcast handlers
)
from .services.export import export_tickets_csv, export_tickets_json
from .services.search import search_comments, search_tickets
from .services.ticket_numbers import next_ticket_number

# ---------------------------------------------------------------------------
# Validated input schemas (BaseModel + VField constraints on every input)
# ---------------------------------------------------------------------------


class AgentLoginSchema(ValidatedModel):
    """POST /auth/agent/login"""

    email: str = VField(min_length=1, max_length=254, strip_whitespace=True)
    password: str = VField(min_length=1)
    org_slug: str = VField(min_length=1, max_length=50, strip_whitespace=True)


class CustomerRegisterSchema(ValidatedModel):
    """POST /auth/customer/register"""

    email: str = VField(min_length=1, max_length=254, strip_whitespace=True)
    display_name: str = VField(min_length=1, max_length=100, strip_whitespace=True)
    password: str = VField(min_length=8)
    org_slug: str = VField(min_length=1, max_length=50, strip_whitespace=True)


class CustomerLoginSchema(ValidatedModel):
    """POST /auth/customer/login"""

    email: str = VField(min_length=1, max_length=254, strip_whitespace=True)
    password: str = VField(min_length=1)
    org_slug: str = VField(min_length=1, max_length=50, strip_whitespace=True)


class TicketCreateSchema(ValidatedModel):
    """POST /tickets/new — agent creates a ticket."""

    title: str = VField(min_length=1, max_length=300, strip_whitespace=True)
    description: str = VField(default="", max_length=10000, strip_whitespace=True)
    priority_id: int = VField(ge=1)
    ticket_type_id: int = VField(ge=1)
    customer_id: int = VField(ge=1)
    team_id: int = VField(default=0, ge=0)
    assignee_id: int = VField(default=0, ge=0)
    source: str = VField(default="web", pattern=r"^(web|email|api|portal)$")


class TicketUpdateSchema(ValidatedModel):
    """POST /tickets/{id}/edit"""

    title: str = VField(default="", max_length=300, strip_whitespace=True)
    description: str = VField(default="", max_length=10000, strip_whitespace=True)
    priority_id: int = VField(default=0, ge=0)
    ticket_type_id: int = VField(default=0, ge=0)


class TicketAssignSchema(ValidatedModel):
    """POST /tickets/{id}/assign"""

    assignee_id: int = VField(default=0, ge=0)
    team_id: int = VField(default=0, ge=0)


class TicketCloseSchema(ValidatedModel):
    """POST /tickets/{id}/close"""

    comment: str = VField(default="", max_length=5000, strip_whitespace=True)


class TicketReopenSchema(ValidatedModel):
    """POST /tickets/{id}/reopen"""

    reason: str = VField(default="", max_length=2000, strip_whitespace=True)


class TicketMergeSchema(ValidatedModel):
    """POST /tickets/{id}/merge"""

    target_ticket_id: int = VField(ge=1)


class TicketSplitSchema(ValidatedModel):
    """POST /tickets/{id}/split"""

    title: str = VField(min_length=1, max_length=300, strip_whitespace=True)
    description: str = VField(default="", max_length=10000, strip_whitespace=True)


class CommentCreateSchema(ValidatedModel):
    """POST /tickets/{id}/comments"""

    body: str = VField(min_length=1, max_length=10000, strip_whitespace=True)
    is_internal: str = VField(default="")  # "on" from checkbox or empty


class CommentUpdateSchema(ValidatedModel):
    """POST /tickets/{id}/comments/{cid}/edit"""

    body: str = VField(min_length=1, max_length=10000, strip_whitespace=True)


class PortalTicketCreateSchema(ValidatedModel):
    """POST /portal/tickets/new — customer submits a ticket."""

    title: str = VField(min_length=1, max_length=300, strip_whitespace=True)
    description: str = VField(min_length=1, max_length=10000, strip_whitespace=True)
    ticket_type_id: int = VField(ge=1)


class PortalCommentSchema(ValidatedModel):
    """POST /portal/tickets/{id}/comment — customer adds a public comment."""

    body: str = VField(min_length=1, max_length=5000, strip_whitespace=True)


class CSATRatingSchema(ValidatedModel):
    """POST /portal/tickets/{id}/rate — customer satisfaction rating."""

    score: int = VField(ge=1, le=5)
    comment: str = VField(default="", max_length=2000, strip_whitespace=True)


class TagCreateSchema(ValidatedModel):
    """POST /tags/new"""

    name: str = VField(
        min_length=1,
        max_length=50,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
        strip_whitespace=True,
        to_lower=True,
    )
    color: str = VField(default="#6b7280", pattern=r"^#[0-9a-fA-F]{6}$")
    description: str = VField(default="", max_length=500, strip_whitespace=True)


class TagApplySchema(ValidatedModel):
    """POST /tickets/{id}/tags/add"""

    tag_id: int = VField(ge=1)


class TeamCreateSchema(ValidatedModel):
    """POST /teams/new"""

    name: str = VField(min_length=1, max_length=100, strip_whitespace=True)
    slug: str = VField(
        min_length=1,
        max_length=50,
        pattern=r"^[a-z0-9_-]+$",
        strip_whitespace=True,
        to_lower=True,
    )
    description: str = VField(default="", max_length=2000, strip_whitespace=True)
    lead_agent_id: int = VField(default=0, ge=0)


class TeamMemberAddSchema(ValidatedModel):
    """POST /teams/{id}/members"""

    agent_id: int = VField(ge=1)
    is_primary: str = VField(default="")  # "on" from checkbox


class AgentCreateSchema(ValidatedModel):
    """POST /agents/new"""

    email: str = VField(min_length=1, max_length=254, strip_whitespace=True)
    display_name: str = VField(min_length=1, max_length=100, strip_whitespace=True)
    password: str = VField(min_length=8)
    role: str = VField(default="agent", pattern=r"^(admin|team_lead|agent)$")


class AgentUpdateSchema(ValidatedModel):
    """POST /agents/{id}/edit"""

    display_name: str = VField(default="", max_length=100, strip_whitespace=True)
    role: str = VField(default="", pattern=r"^(admin|team_lead|agent|)$")


class ApplyTemplateSchema(ValidatedModel):
    """POST /tickets/{id}/apply-template"""

    template_id: int = VField(ge=1)


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

_APP_DIR = Path(__file__).resolve().parent
_DEBUG = get_setting("DEBUG")
_site_config = load_hyperticket_config()

# Set per-app defaults (DEFAULTS tier — env vars still override)
DEFAULTS["DATABASE_URL"] = (
    get_setting("DATABASE_URL") or "postgres://localhost/hyperdjango_test"
)

app = HyperApp(
    database=get_setting("DATABASE_URL"),
    templates=str(_APP_DIR / "templates"),
    static=str(_APP_DIR / "static"),
    debug=_DEBUG,
    secret_key=get_setting("SECRET_KEY"),
    site_config=_site_config,
)

# --- Native telemetry (v0.15.1) -----------------------------------------------
if _DEBUG:
    DEFAULTS["TELEMETRY_ENABLED"] = True
    DEFAULTS["TELEMETRY_SAMPLE_RATIO"] = 1.0
_telemetry = configure_from_settings(app)
if _telemetry is not None and _telemetry.prometheus_sink is not None:
    app.get("/metrics")(_telemetry.prometheus_sink.handler)

# Middleware stack (outermost first)
app.use(TimingMiddleware())
app.use(SecurityHeadersMiddleware(hsts=not _DEBUG))
app.use(
    CORSMiddleware(
        origins=["*"],
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        headers=["Content-Type"],
    )
)
csrf = CSRFMiddleware(
    secret=get_setting("CSRF_SECRET"),
    exempt_paths=set(),
    exempt_prefixes={
        "/admin/",
        "/auth/",
    },  # Admin has own CSRF; auth forms set cookie on GET
)
app.use(csrf)
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


def _resolve_tenant_from_session(request) -> TenantRef | None:
    """Resolve tenant from session data — set during login.

    No headers, no URL params — tenant identity comes from the authenticated
    session, which stores tenant_id when the agent or customer logs in.
    """
    user = request.user
    if user is None or not user.is_authenticated:
        return None
    tenant_id = user.get("tenant_id")
    if not tenant_id:
        return None
    return TenantRef(tenant_id=int(tenant_id))


app.use(TenantMiddleware(resolve_tenant=_resolve_tenant_from_session))


@app.exception_handler(Exception)
async def _handle_error(request, exc):
    logger.exception("Unhandled error: {err}", err=str(exc))
    return Response.json({"detail": "Internal server error"}, status=500)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


@app.on_startup
async def _startup():
    try:
        org_count = await Org.objects.count()
        await get_timeline().ensure_indexes()
        ticket_count = await Ticket.objects.count()
        logger.info(
            "HyperTicket ready: {orgs} orgs, {tickets} tickets",
            orgs=org_count,
            tickets=ticket_count,
        )
    except Exception:
        logger.info("HyperTicket: tables not yet created (run hyper setup)")

    # Register adapters globally
    adapter_registry.register_ticket_adapter(AITriageAdapter())
    adapter_registry.register_comment_adapter(AIContentModerationAdapter())


# Health
app.mount_health()

# WebSocket endpoint
register_ws_endpoint(app, auth)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def validate_form(request, schema_cls: type):
    """Parse form data and validate against a BaseModel schema.

    Flattens multi-value form data, uses model_validate_strings() for
    automatic type coercion with Zig-accelerated validation.
    """
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


async def validate_json(request, schema_cls: type):
    """Parse JSON body and validate against a BaseModel schema."""
    data = await request.json()
    return schema_cls.model_validate(data)


@time_bucket_cached(bucket_seconds=30)
def time_ago(timestamp_str) -> str:
    """Convert a timestamp to a compact human-readable 'Xm ago' string.

    Cached within a 30-second bucket — the same timestamp passed
    multiple times within 30s returns the cached string. Ticket list
    pages hit this many times per request with repeated timestamps.
    """
    if not timestamp_str:
        return ""
    try:
        if isinstance(timestamp_str, str):
            ts = datetime.fromisoformat(timestamp_str)
        else:
            ts = timestamp_str
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        now = datetime.now(UTC)
        diff = now - ts
        seconds = int(diff.total_seconds())
        if seconds < 60:
            return "just now"
        minutes = seconds // 60
        if minutes < 60:
            return f"{minutes}m ago"
        hours = minutes // 60
        if hours < 24:
            return f"{hours}h ago"
        days = hours // 24
        if days < 30:
            return f"{days}d ago"
        months = days // 30
        return f"{months}mo ago"
    except ValueError, TypeError:
        return ""


async def paginate(qs, request, page_size: int = 25, ordering: str = "-id"):
    """Paginate an ORM queryset using CursorPagination.

    Returns (items, next_cursor, prev_cursor) for template rendering.
    """
    paginator = CursorPagination()
    paginator.page_size = page_size
    paginator.ordering = ordering
    items = await paginator.paginate_queryset(qs, request)
    next_cursor = paginator._next_cursor or ""
    prev_cursor = paginator._prev_cursor or ""
    return items, next_cursor, prev_cursor


def get_agent_id(request) -> int:
    """Extract agent ID from session. Returns 0 if not an agent."""
    user = request.user
    if user is not None and user.get("user_type") == "agent":
        return user.id or 0
    return 0


def get_customer_id(request) -> int:
    """Extract customer ID from session. Returns 0 if not a customer."""
    user = request.user
    if user is not None and user.get("user_type") == "customer":
        return user.id or 0
    return 0


def build_context(request, **extra) -> dict[str, object]:
    """Build a template context dict with common fields."""
    user = request.user
    user_type = user.get("user_type", "") if user is not None else ""
    tenant = get_tenant()
    return {
        "user": user,
        "user_type": user_type,
        "is_agent": user_type == "agent",
        "is_customer": user_type == "customer",
        "tenant_id": tenant.tenant_id if tenant else 0,
        "time_ago": time_ago,
        "site": _site_config,
        "site_css": _site_config.theme.to_css_vars(),
        "csrf_token": request.cookies.get("csrftoken", ""),
        **extra,
    }


async def build_themed_context(request, **extra) -> dict[str, object]:
    """Build context with per-tenant SiteConfig overlay from TenantTheme model.

    Uses dataclasses.replace() to create an immutable overlay of the base
    _site_config with tenant-specific branding from the database.
    """
    ctx = build_context(request, **extra)
    tenant = get_tenant()
    if tenant:
        theme = await TenantTheme.objects.filter(tenant_id=tenant.tenant_id).first()
        if theme:
            # Create per-tenant ThemeColors overlay from DB values
            tenant_theme = dataclasses.replace(
                _site_config.theme,
                primary=theme.primary_color,
                background=theme.background_color,
                surface=theme.secondary_color,
                text=theme.text_color,
                header_bg=theme.header_background,
                header_text=theme.header_text_color,
            )
            # Create per-tenant SiteConfig overlay
            tenant_config = dataclasses.replace(
                _site_config,
                name=theme.company_name_display or _site_config.name,
                logo_url=theme.logo_url or _site_config.logo_url,
                favicon_url=theme.favicon_url or _site_config.favicon_url,
                font_family=theme.font_family or _site_config.font_family,
                theme=tenant_theme,
            )
            ctx["site"] = tenant_config
            ctx["site_css"] = tenant_theme.to_css_vars()
            ctx["custom_css"] = theme.custom_css
    return ctx


# ---------------------------------------------------------------------------
# Guard infrastructure — intent-driven resolvers
# ---------------------------------------------------------------------------


# Role precedence — indexed by enum instance (ORM hydrates enum fields as
# enum instances, not raw strings). The old string-keyed dict silently
# returned -1 for enum lookups and gated every admin action behind a 403.
_ROLE_LEVEL: dict[AgentRole, int] = {
    AgentRole.AGENT: 0,
    AgentRole.TEAM_LEAD: 1,
    AgentRole.ADMIN: 2,
}


def _role_at_least(role: AgentRole | str, minimum: AgentRole) -> bool:
    """Check if `role` meets the `minimum` role requirement.

    Accepts both AgentRole instances (the normal case, from ORM hydration)
    and raw strings (for backward compat with templates or raw SQL paths).
    """
    if not isinstance(role, AgentRole):
        try:
            role = AgentRole(role)
        except ValueError, TypeError:
            return False
    return _ROLE_LEVEL.get(role, -1) >= _ROLE_LEVEL[minimum]


async def _resolve_agent(request, ctx):
    """Load Agent from session, verify tenant match + active status.

    Used as guard resource resolver → request.guard.agent.
    """
    user = request.user
    if user is None or not user.is_authenticated or user.get("user_type") != "agent":
        return None
    agent_id = user.id
    if agent_id is None:
        return None
    agent = await Agent.objects.filter(id=agent_id).first()
    if not agent:
        return None
    tenant = get_tenant()
    if tenant and agent.tenant_id != tenant.tenant_id:
        return None  # Agent doesn't belong to this tenant
    if await agent.has_status("lifecycle", "deactivated"):
        raise HTTPException(403, "Your account has been deactivated")
    return agent


async def _resolve_customer(request, ctx):
    """Load Customer from session, verify tenant match."""
    user = request.user
    if user is None or not user.is_authenticated or user.get("user_type") != "customer":
        return None
    customer_id = user.id
    if customer_id is None:
        return None
    customer = await Customer.objects.filter(id=customer_id).first()
    if not customer:
        return None
    tenant = get_tenant()
    if tenant and customer.tenant_id != tenant.tenant_id:
        return None
    return customer


# Reusable guard chains
REQUIRE_AGENT = (
    Require.authenticated(redirect_url="/auth/agent/login"),
    Require.resource("agent", resolver=_resolve_agent),
)


def _make_role_check(min_role: AgentRole):
    """Create a guard check that requires at least the given role level."""

    async def _check(request, ctx):
        agent = ctx.resources.get("agent")
        if agent is None:
            return GuardDenial(DenyReason.FORBIDDEN, "Agent not found")
        if not _role_at_least(agent.role, min_role):
            return GuardDenial(
                DenyReason.FORBIDDEN,
                f"Requires {min_role.value} role or higher",
            )
        return None

    return _check


REQUIRE_TEAM_LEAD = (
    Require.authenticated(redirect_url="/auth/agent/login"),
    Require.resource("agent", resolver=_resolve_agent),
    Require.role("team_lead"),
)

REQUIRE_ADMIN = (
    Require.authenticated(redirect_url="/auth/agent/login"),
    Require.resource("agent", resolver=_resolve_agent),
    Require.role("admin"),
)

REQUIRE_CUSTOMER = (
    Require.authenticated(redirect_url="/auth/customer/login"),
    Require.resource("customer", resolver=_resolve_customer),
)


# ---------------------------------------------------------------------------
# Intent-based ticket access control
# ---------------------------------------------------------------------------


class TicketIntent(Enum):
    """What the caller intends to do with the ticket.

    Each intent implies role-based and ownership-based checks.
    The resolver enforces all checks in one call.
    """

    READ = "read"  # View ticket detail + comments — any agent
    UPDATE = "update"  # Edit title/description — assignee, team member, or team_lead+
    ASSIGN = "assign"  # Change assignee/team — team_lead+ or admin
    CLOSE = "close"  # Close ticket — assignee or team_lead+
    REOPEN = "reopen"  # Reopen — any agent
    LOCK = "lock"  # Lock/unlock — team_lead+
    MERGE = "merge"  # Merge into another — team_lead+
    DELETE = "delete"  # Soft delete — admin only


@dataclass
class TicketAccess:
    """Result of resolving and validating ticket access by intent."""

    ticket: Ticket
    agent: Agent
    is_assignee: bool
    is_team_member: bool


# Intent → minimum role (None = any agent, but may require assignee/team check)
_TICKET_INTENT_ROLES: dict[TicketIntent, AgentRole | None] = {
    TicketIntent.READ: None,
    TicketIntent.UPDATE: None,  # checked via assignee/team membership
    TicketIntent.ASSIGN: AgentRole.TEAM_LEAD,
    TicketIntent.CLOSE: None,  # assignee or team_lead+
    TicketIntent.REOPEN: None,
    TicketIntent.LOCK: AgentRole.TEAM_LEAD,
    TicketIntent.MERGE: AgentRole.TEAM_LEAD,
    TicketIntent.DELETE: AgentRole.ADMIN,
}


async def resolve_ticket(
    request, ticket_id: int, intent: TicketIntent, agent: Agent | None = None
) -> TicketAccess:
    """Fetch a ticket and enforce intent-specific access control.

    Centralizes: fetch → tenant check → soft-delete check → role check →
    ownership check into ONE call.

    agent: pre-resolved from guard context to avoid double-fetch.

    Uses `join_related` (non-destructive variant of `select_related`,
    task #196) to eager-load the 4 real-FK relations (status,
    priority, ticket_type, customer) in a single SQL roundtrip via
    JOINs. The FK integer columns (status_id, priority_id, etc.) are
    PRESERVED as ints, so all downstream handlers that read them for
    equality checks, filter kwargs, and Activity log entries keep
    working unchanged. Instances are attached on sibling attributes:
    `ticket.status`, `ticket.priority`, `ticket.ticket_type`,
    `ticket.customer`. Collapses 4 follow-up SELECT queries per
    /tickets/{id} view into the main query.

    `assignee_id` + `team_id` are logical FKs (`int = Field(default=0)`
    with `0 = unassigned` convention) so they aren't eligible for
    join_related and remain as separate queries in `ticket_detail`.
    """
    ticket = (
        await Ticket.objects.join_related(
            status="status_id",
            priority="priority_id",
            ticket_type="ticket_type_id",
            customer="customer_id",
        )
        .filter(id=ticket_id)
        .first()
    )
    if not ticket:
        raise HTTPException(404, "Ticket not found")

    # Tenant isolation
    tenant = get_tenant()
    if tenant and ticket.tenant_id != tenant.tenant_id:
        raise HTTPException(404, "Ticket not found")

    # Soft-deleted tickets not accessible (except admin for restore)
    if ticket.is_deleted and intent != TicketIntent.DELETE:
        raise HTTPException(404, "Ticket not found")

    # Use pre-resolved agent from guard, or load from session as fallback
    if not agent:
        agent_id = get_agent_id(request)
        agent = await Agent.objects.filter(id=agent_id).first()
    if not agent:
        raise HTTPException(403, "Agent not found")

    is_assignee = ticket.assignee_id == agent.id
    is_team_member = False
    if ticket.team_id:
        membership = await TeamMembership.objects.filter(
            team_id=ticket.team_id, agent_id=agent.id
        ).first()
        is_team_member = membership is not None

    # Check minimum role requirement for this intent
    min_role = _TICKET_INTENT_ROLES[intent]
    if min_role is not None:
        if not _role_at_least(agent.role, min_role):
            raise HTTPException(403, f"Requires {min_role.value} role or higher")

    # Ownership-based checks for UPDATE and CLOSE intents
    if intent == TicketIntent.UPDATE:
        if not (
            is_assignee
            or is_team_member
            or _role_at_least(agent.role, AgentRole.TEAM_LEAD)
        ):
            raise HTTPException(
                403, "Only assignee, team member, or team lead can update"
            )

    if intent == TicketIntent.CLOSE:
        if not (is_assignee or _role_at_least(agent.role, AgentRole.TEAM_LEAD)):
            raise HTTPException(403, "Only assignee or team lead can close")

    return TicketAccess(ticket, agent, is_assignee, is_team_member)


def _make_ticket_resolver(intent: TicketIntent):
    """Create a guard resource resolver for a specific ticket intent.

    Passes the already-resolved agent from ctx to avoid double DB fetch.
    """

    async def resolver(request, ctx, ticket_id):
        agent = ctx.resources.get("agent")
        return await resolve_ticket(request, int(ticket_id), intent, agent=agent)

    return resolver


_resolve_ticket_read = _make_ticket_resolver(TicketIntent.READ)
_resolve_ticket_update = _make_ticket_resolver(TicketIntent.UPDATE)
_resolve_ticket_assign = _make_ticket_resolver(TicketIntent.ASSIGN)
_resolve_ticket_close = _make_ticket_resolver(TicketIntent.CLOSE)
_resolve_ticket_reopen = _make_ticket_resolver(TicketIntent.REOPEN)
_resolve_ticket_lock = _make_ticket_resolver(TicketIntent.LOCK)
_resolve_ticket_merge = _make_ticket_resolver(TicketIntent.MERGE)
_resolve_ticket_delete = _make_ticket_resolver(TicketIntent.DELETE)


# Portal: customer can only access own tickets
async def _resolve_portal_ticket(request, ctx, ticket_id):
    """Resolve ticket for customer portal — own tickets only, public comments only."""
    ticket = await Ticket.objects.filter(id=int(ticket_id)).first()
    if not ticket:
        raise HTTPException(404, "Ticket not found")
    tenant = get_tenant()
    if tenant and ticket.tenant_id != tenant.tenant_id:
        raise HTTPException(404, "Ticket not found")
    if ticket.is_deleted:
        raise HTTPException(404, "Ticket not found")
    customer_id = get_customer_id(request)
    if ticket.customer_id != customer_id:
        raise HTTPException(404, "Ticket not found")  # IDOR prevention — 404 not 403
    return ticket


# ---------------------------------------------------------------------------
# Activity logging helper
# ---------------------------------------------------------------------------


async def log_activity(
    tenant_id: int,
    ticket_id: int,
    actor_type: str,
    actor_id: int,
    action: ActivityAction,
    detail: str = "{}",
) -> None:
    """Record an activity log entry for a ticket."""
    await ActivityLog(
        tenant_id=tenant_id,
        ticket_id=ticket_id,
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        detail=detail,
    ).save()


# ---------------------------------------------------------------------------
# HyperAdmin — register all 27 models
# ---------------------------------------------------------------------------

admin = HyperAdmin(
    app,
    prefix="/admin",
    title=f"{_site_config.name} Admin",
    secret_key=get_setting("ADMIN_SECRET"),
)


# Admin actions — batch update, no per-PK loops
async def _deactivate_agents(adm, config, selected_ids, request):
    int_ids = [int(pk) for pk in selected_ids]
    tl = get_timeline()
    for aid in int_ids:
        await tl.add_event(
            "agent", aid, "lifecycle", "deactivated", reason="Admin bulk deactivation"
        )
    return f"Deactivated {len(selected_ids)} agent(s)"


async def _activate_agents(adm, config, selected_ids, request):
    int_ids = [int(pk) for pk in selected_ids]
    tl = get_timeline()
    for aid in int_ids:
        await tl.end_status(
            "agent", aid, "lifecycle", end_reason="Admin bulk activation"
        )
    return f"Activated {len(selected_ids)} agent(s)"


async def _deactivate_orgs(adm, config, selected_ids, request):
    int_ids = [int(pk) for pk in selected_ids]
    await Org.objects.filter(id__in=int_ids).update(is_active=False)
    return f"Deactivated {len(selected_ids)} org(s)"


async def _verify_customers(adm, config, selected_ids, request):
    int_ids = [int(pk) for pk in selected_ids]
    await Customer.objects.filter(id__in=int_ids).update(is_verified=True)
    return f"Verified {len(selected_ids)} customer(s)"


# Register models
admin.register(
    Org,
    list_display=["id", "name", "slug", "is_active", "plan_config_id", "created_at"],
    search_fields=["name", "slug"],
    list_filter=["is_active"],
    ordering="-id",
    actions=[
        Action(
            name="deactivate", label="Deactivate selected", handler=_deactivate_orgs
        ),
    ],
)

admin.register(
    PlanConfig,
    list_display=["id", "name", "is_public", "display_order", "base_price_cents"],
    search_fields=["name"],
    ordering="display_order",
)

admin.register(
    PlanFeatureLimit,
    list_display=["id", "plan_config_id", "feature_key", "limit_value", "enforcement"],
    list_filter=["enforcement"],
    ordering="plan_config_id",
)

admin.register(
    Agent,
    list_display=["id", "email", "display_name", "role", "created_at"],
    search_fields=["email", "display_name"],
    list_filter=["role"],
    ordering="-id",
    actions=[
        Action(name="deactivate", label="Deactivate", handler=_deactivate_agents),
        Action(name="activate", label="Activate", handler=_activate_agents),
    ],
)

admin.register(
    Customer,
    list_display=["id", "email", "display_name", "is_verified", "created_at"],
    search_fields=["email", "display_name"],
    list_filter=["is_verified"],
    ordering="-id",
    actions=[
        Action(name="verify", label="Verify selected", handler=_verify_customers),
    ],
)

admin.register(
    Team,
    list_display=["id", "name", "slug", "lead_agent_id", "is_active"],
    search_fields=["name", "slug"],
    list_filter=["is_active"],
    ordering="name",
)

admin.register(
    TeamMembership,
    list_display=["id", "team_id", "agent_id", "is_primary"],
    ordering="-id",
)
admin.register(
    AgentSkill,
    list_display=["id", "agent_id", "skill_tag", "proficiency"],
    ordering="-id",
)

admin.register(
    Ticket,
    list_display=[
        "id",
        "ticket_number",
        "title",
        "status_id",
        "priority_id",
        "assignee_id",
        "created_at",
    ],
    search_fields=["ticket_number", "title"],
    list_filter=["status_id", "priority_id", "team_id"],
    ordering="-id",
)

admin.register(
    TicketStatusConfig,
    list_display=[
        "id",
        "slug",
        "label",
        "color",
        "category",
        "is_default",
        "is_terminal",
        "sort_order",
    ],
    list_filter=["category", "is_active"],
    ordering="sort_order",
)

admin.register(
    StatusTransition,
    list_display=["id", "from_status_id", "to_status_id", "requires_role"],
    ordering="-id",
)
admin.register(
    PriorityConfig,
    list_display=["id", "slug", "label", "color", "sla_multiplier", "sort_order"],
    ordering="sort_order",
)
admin.register(
    TicketTypeConfig,
    list_display=["id", "slug", "label", "color", "sort_order"],
    ordering="sort_order",
)

admin.register(
    Tag, list_display=["id", "name", "color"], search_fields=["name"], ordering="name"
)
admin.register(TicketTag, list_display=["id", "ticket_id", "tag_id"], ordering="-id")
admin.register(
    TicketRelation,
    list_display=["id", "source_ticket_id", "target_ticket_id", "relation_type"],
    ordering="-id",
)

admin.register(
    Comment,
    list_display=[
        "id",
        "ticket_id",
        "author_type",
        "author_id",
        "is_internal",
        "created_at",
    ],
    list_filter=["author_type", "is_internal"],
    ordering="-id",
)

admin.register(
    Attachment,
    list_display=["id", "ticket_id", "filename", "content_type", "size_bytes"],
    ordering="-id",
)
admin.register(
    CannedResponse,
    list_display=["id", "title", "shortcut", "category"],
    search_fields=["title", "shortcut"],
    ordering="title",
)
admin.register(
    SatisfactionRating,
    list_display=["id", "ticket_id", "customer_id", "score"],
    ordering="-id",
)

admin.register(
    SLAPolicy,
    list_display=[
        "id",
        "name",
        "is_default",
        "first_response_normal",
        "resolution_normal",
    ],
    ordering="-id",
)
admin.register(
    SLAInstance,
    list_display=[
        "id",
        "ticket_id",
        "sla_policy_id",
        "breached",
        "first_response_met",
        "resolution_met",
    ],
    ordering="-id",
)
admin.register(
    EscalationRule,
    list_display=["id", "name", "trigger_type", "is_active"],
    ordering="-id",
)

admin.register(
    WorkflowRule,
    list_display=["id", "name", "trigger_event", "is_active", "execution_order"],
    ordering="execution_order",
)
admin.register(
    Approval, list_display=["id", "ticket_id", "status", "approver_id"], ordering="-id"
)
admin.register(
    TicketTemplate,
    list_display=["id", "name", "ticket_type_id", "priority_id"],
    search_fields=["name"],
    ordering="name",
)
admin.register(
    SavedView, list_display=["id", "name", "agent_id", "is_default"], ordering="name"
)
admin.register(
    ActivityLog,
    list_display=["id", "ticket_id", "actor_type", "action", "created_at"],
    ordering="-id",
)
admin.register(
    NotificationPreference,
    list_display=["id", "agent_id", "event_type", "channel_email", "channel_in_app"],
    ordering="-id",
)

admin.register(
    OrgSettings,
    list_display=["id", "tenant_id", "timezone", "auto_assignment_strategy"],
    ordering="-id",
)
admin.register(
    OrgAPIKey,
    list_display=["id", "name", "key_prefix", "is_active", "created_at"],
    ordering="-id",
)
admin.register(
    TenantTheme,
    list_display=["id", "tenant_id", "company_name_display", "primary_color"],
    ordering="-id",
)

register_timeline_admin(admin)
admin.register_auth_models()
admin.register_ratelimit_models()
admin.register_cache_dashboard()


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------


@app.get("/")
async def root_redirect(request):
    """Redirect root to the appropriate landing page based on auth state."""
    user = request.user
    if user is not None:
        user_type = user.get("user_type", "")
        if user_type == "agent":
            return Response.redirect("/dashboard/")
        if user_type == "customer":
            return Response.redirect("/portal/")
    return Response.redirect("/auth/agent/login")


@app.get("/auth/agent/login")
async def agent_login_form(request):
    ctx = build_context(request)
    return app.render("agent_login.html", ctx)


@app.post("/auth/agent/login")
async def agent_login_handler(request):
    try:
        data = await validate_form(request, AgentLoginSchema)
    except ValidationErrors as exc:
        ctx = build_context(request, errors=[str(e) for e in exc.errors])
        return app.render("agent_login.html", ctx, status=400)

    client_ip = request.client_ip or "unknown"
    if auth.is_login_blocked(client_ip):
        raise HTTPException(429, "Too many login attempts")

    # Look up org by slug (no session → no tenant context yet)
    org = await Org.objects.filter(slug=data.org_slug, is_active=True).first()
    if not org:
        ctx = build_context(request, errors=["Organization not found"])
        return app.render("agent_login.html", ctx, status=400)

    # Search agent within org (unscoped since no tenant context)
    agent = (
        await Agent.objects.unscoped()
        .filter(email=data.email, tenant_id=org.id)
        .first()
    )
    if not agent or not verify_password(data.password, agent.password_hash):
        auth.record_failed_login(client_ip)
        ctx = build_context(request, errors=["Invalid email or password"])
        return app.render("agent_login.html", ctx, status=400)

    if await agent.has_status("lifecycle", "deactivated"):
        ctx = build_context(request, errors=["Your account has been deactivated"])
        return app.render("agent_login.html", ctx, status=403)

    auth.clear_login_attempts(client_ip)

    # Map AgentRole to RBAC groups for guard system.
    # admin → ["admin", "team_lead", "agent"] (inherits all lower roles)
    # team_lead → ["team_lead", "agent"]
    # agent → ["agent"]
    role_val = agent.role.value if isinstance(agent.role, AgentRole) else agent.role
    _agent_role_groups: dict[str, list[str]] = {
        "admin": ["admin", "team_lead", "agent"],
        "team_lead": ["team_lead", "agent"],
        "agent": ["agent"],
    }
    groups = _agent_role_groups.get(role_val, ["agent"])

    session = await build_session_data(
        agent.id,
        get_db(),
        groups=groups,
        email=agent.email,
        display_name=agent.display_name,
        # Keep role in session for display purposes
        role=role_val,
        user_type="agent",
        tenant_id=agent.tenant_id,
    )
    resp = Response.redirect("/dashboard/")
    auth.login(resp, session, request)
    return resp


@app.post("/auth/agent/logout")
async def agent_logout(request):
    resp = Response.redirect("/auth/agent/login")
    # SessionAuth.logout takes the session_id (a string), NOT the
    # request object. Previous bug: `auth.logout(resp, request)`
    # passed a Request instance to `store.delete(session_id)`, which
    # silently no-op'd the server-side session delete. The cookie
    # was cleared on the wire but the old session_id remained valid
    # in the DB until TTL — logout-then-reuse was a real vector.
    auth.logout(resp, request.session_id)
    return resp


@app.get("/auth/customer/register")
async def customer_register_form(request):
    ctx = build_context(request)
    return app.render("customer_register.html", ctx)


@app.post("/auth/customer/register")
async def customer_register_handler(request):
    try:
        data = await validate_form(request, CustomerRegisterSchema)
    except ValidationErrors as exc:
        ctx = build_context(request, errors=[str(e) for e in exc.errors])
        return app.render("customer_register.html", ctx, status=400)

    # Look up org by slug (no session yet — registration is pre-auth)
    org = await Org.objects.filter(slug=data.org_slug, is_active=True).first()
    if not org:
        ctx = build_context(request, errors=["Organization not found"])
        return app.render("customer_register.html", ctx, status=400)

    # Check email uniqueness within org (use unscoped since no tenant context yet)
    existing = (
        await Customer.objects.unscoped()
        .filter(email=data.email, tenant_id=org.id)
        .first()
    )
    if existing:
        ctx = build_context(request, errors=["Email already registered"])
        return app.render("customer_register.html", ctx, status=400)

    customer = Customer(
        tenant_id=org.id,
        email=data.email,
        display_name=data.display_name,
        password_hash=hash_password(data.password),
        is_verified=True,  # Auto-verify in demo; production would email-verify
    )
    await customer.save()

    session = await build_session_data(
        customer.id,
        get_db(),
        groups=["customer"],
        email=customer.email,
        display_name=customer.display_name,
        user_type="customer",
        tenant_id=customer.tenant_id,
    )
    resp = Response.redirect("/portal/")
    auth.login(resp, session, request)
    return resp


@app.get("/auth/customer/login")
async def customer_login_form(request):
    ctx = build_context(request)
    return app.render("customer_login.html", ctx)


@app.post("/auth/customer/login")
async def customer_login_handler(request):
    try:
        data = await validate_form(request, CustomerLoginSchema)
    except ValidationErrors as exc:
        ctx = build_context(request, errors=[str(e) for e in exc.errors])
        return app.render("customer_login.html", ctx, status=400)

    client_ip = request.client_ip or "unknown"
    if auth.is_login_blocked(client_ip):
        raise HTTPException(429, "Too many login attempts")

    # Look up org by slug (no session → no tenant context yet)
    org = await Org.objects.filter(slug=data.org_slug, is_active=True).first()
    if not org:
        ctx = build_context(request, errors=["Organization not found"])
        return app.render("customer_login.html", ctx, status=400)

    customer = (
        await Customer.objects.unscoped()
        .filter(email=data.email, tenant_id=org.id)
        .first()
    )
    if not customer or not verify_password(data.password, customer.password_hash):
        auth.record_failed_login(client_ip)
        ctx = build_context(request, errors=["Invalid email or password"])
        return app.render("customer_login.html", ctx, status=400)

    auth.clear_login_attempts(client_ip)
    session = await build_session_data(
        customer.id,
        get_db(),
        groups=["customer"],
        email=customer.email,
        display_name=customer.display_name,
        user_type="customer",
        tenant_id=customer.tenant_id,
    )
    resp = Response.redirect("/portal/")
    auth.login(resp, session, request)
    return resp


@app.post("/auth/customer/logout")
async def customer_logout(request):
    resp = Response.redirect("/auth/customer/login")
    # See agent_logout above — session_id, not request.
    auth.logout(resp, request.session_id)
    return resp


# ---------------------------------------------------------------------------
# Agent ticket routes
# ---------------------------------------------------------------------------


@app.get("/tickets/")
@guard(*REQUIRE_AGENT)
async def ticket_list(request):
    """List tickets for the current tenant. Supports filtering by query params."""
    agent = request.guard.agent

    qs = Ticket.objects.order_by("-id")

    # Apply filters from query params
    status_id = request.GET.get("status_id")
    if status_id:
        qs = qs.filter(status_id=int(status_id))

    priority_id = request.GET.get("priority_id")
    if priority_id:
        qs = qs.filter(priority_id=int(priority_id))

    assignee_id = request.GET.get("assignee_id")
    if assignee_id:
        qs = qs.filter(assignee_id=int(assignee_id))

    team_id = request.GET.get("team_id")
    if team_id:
        qs = qs.filter(team_id=int(team_id))

    # Cursor pagination
    tickets, next_cursor, prev_cursor = await paginate(qs, request, page_size=25)

    # Fetch status/priority labels for display
    statuses = await TicketStatusConfig.objects.all()
    status_map = {s.id: s for s in statuses}
    priorities = await PriorityConfig.objects.all()
    priority_map = {p.id: p for p in priorities}

    ctx = await build_themed_context(
        request,
        tickets=tickets,
        status_map=status_map,
        priority_map=priority_map,
        agent=agent,
        next_cursor=next_cursor,
        prev_cursor=prev_cursor,
    )

    if request.headers.get("hx-request"):
        return app.render("_partials/ticket_list_rows.html", ctx)
    return app.render("ticket_list.html", ctx)


@app.get("/tickets/new")
@guard(*REQUIRE_AGENT)
async def ticket_create_form(request):
    """Show ticket creation form."""
    statuses = (
        await TicketStatusConfig.objects.filter(is_active=True)
        .order_by("sort_order")
        .all()
    )
    priorities = (
        await PriorityConfig.objects.filter(is_active=True).order_by("sort_order").all()
    )
    types = (
        await TicketTypeConfig.objects.filter(is_active=True)
        .order_by("sort_order")
        .all()
    )
    customers = await Customer.objects.all()
    teams = await Team.objects.filter(is_active=True).all()

    ctx = build_context(
        request,
        statuses=statuses,
        priorities=priorities,
        types=types,
        customers=customers,
        teams=teams,
    )
    return app.render("ticket_form.html", ctx)


@app.post("/tickets/new")
@guard(*REQUIRE_AGENT)
async def ticket_create_handler(request):
    """Create a new ticket."""
    agent = request.guard.agent

    try:
        data = await validate_form(request, TicketCreateSchema)
    except ValidationErrors as exc:
        ctx = build_context(request, errors=[str(e) for e in exc.errors])
        return app.render("ticket_form.html", ctx, status=400)

    # Guard already validated agent + tenant
    tenant_id = agent.tenant_id

    # Validate FK references exist within tenant
    status_config = await TicketStatusConfig.objects.filter(is_default=True).first()
    if not status_config:
        raise HTTPException(500, "No default status configured")

    priority = await PriorityConfig.objects.filter(id=data.priority_id).first()
    if not priority:
        raise HTTPException(400, "Invalid priority")

    ticket_type = await TicketTypeConfig.objects.filter(id=data.ticket_type_id).first()
    if not ticket_type:
        raise HTTPException(400, "Invalid ticket type")

    customer = await Customer.objects.filter(id=data.customer_id).first()
    if not customer:
        raise HTTPException(400, "Invalid customer")

    ticket_number = await next_ticket_number(tenant_id)

    ticket = Ticket(
        tenant_id=tenant_id,
        ticket_number=ticket_number,
        title=data.title,
        description=data.description,
        status_id=status_config.id,
        priority_id=data.priority_id,
        ticket_type_id=data.ticket_type_id,
        customer_id=data.customer_id,
        assignee_id=data.assignee_id,
        team_id=data.team_id,
        source=TicketSource(data.source),
    )
    await ticket.save()
    # Note: ActivityLog CREATED entry handled by post_save signal in signals.py

    return Response.redirect(f"/tickets/{ticket.id}")


@app.get("/tickets/{ticket_id:int}")
@guard(
    *REQUIRE_AGENT,
    Require.resource("access", resolver=_resolve_ticket_read, from_path="ticket_id"),
)
async def ticket_detail(request, ticket_id: int):
    """Show ticket detail with comments, activity, SLA status."""
    access = request.guard.access
    ticket = access.ticket

    # Fetch related data
    comments = (
        await Comment.objects.filter(ticket_id=ticket.id)
        .order_by("id")
        .limit(200)
        .all()
    )
    tags = await TicketTag.objects.filter(ticket_id=ticket.id).all()
    tag_ids = [tt.tag_id for tt in tags]
    tag_objects = await Tag.objects.filter(id__in=tag_ids).all() if tag_ids else []

    # Status/priority/type/customer are already loaded by
    # resolve_ticket()'s join_related() — read from the attached
    # sibling attributes. 4 follow-up SELECTs collapsed into the
    # single JOIN query resolve_ticket already ran.
    status = ticket.status
    priority = ticket.priority
    ticket_type = ticket.ticket_type
    customer = ticket.customer

    # Assignee is a logical FK (assignee_id: int with 0 = unassigned)
    # and not a real ORM FK, so it can't be join_related. Keep it as
    # a separate conditional query.
    assignee = (
        await Agent.objects.filter(id=ticket.assignee_id).first()
        if ticket.assignee_id
        else None
    )

    # Activity timeline (recent 50)
    activity = (
        await ActivityLog.objects.filter(ticket_id=ticket.id)
        .order_by("-id")
        .limit(50)
        .all()
    )

    # Available statuses for transition
    transitions = await StatusTransition.objects.filter(
        from_status_id=ticket.status_id
    ).all()
    to_status_ids = [t.to_status_id for t in transitions]
    available_statuses = (
        await TicketStatusConfig.objects.filter(id__in=to_status_ids).all()
        if to_status_ids
        else []
    )

    # Resolve timeline-based flags for template rendering (templates can't call async)
    is_locked = await ticket.has_status("state", "locked")
    is_muted = await ticket.has_status("state", "muted")

    ctx = build_context(
        request,
        ticket=ticket,
        comments=comments,
        tags=tag_objects,
        status=status,
        priority=priority,
        ticket_type=ticket_type,
        assignee=assignee,
        customer=customer,
        activity=activity,
        available_statuses=available_statuses,
        access=access,
        is_locked=is_locked,
        is_muted=is_muted,
    )
    return app.render("ticket_detail.html", ctx)


@app.post("/tickets/{ticket_id:int}/edit")
@guard(
    *REQUIRE_AGENT,
    Require.resource("access", resolver=_resolve_ticket_update, from_path="ticket_id"),
)
async def ticket_edit_handler(request, ticket_id: int):
    """Update ticket fields."""
    access = request.guard.access
    ticket = access.ticket

    try:
        data = await validate_form(request, TicketUpdateSchema)
    except ValidationErrors as exc:
        raise HTTPException(400, str(exc.errors))

    updates: dict[str, object] = {}
    if data.title:
        updates["title"] = data.title
    if data.description:
        updates["description"] = data.description
    if data.priority_id:
        priority = await PriorityConfig.objects.filter(id=data.priority_id).first()
        if not priority:
            raise HTTPException(400, "Invalid priority")
        updates["priority_id"] = data.priority_id
    if data.ticket_type_id:
        tt = await TicketTypeConfig.objects.filter(id=data.ticket_type_id).first()
        if not tt:
            raise HTTPException(400, "Invalid ticket type")
        updates["ticket_type_id"] = data.ticket_type_id

    if updates:
        await Ticket.objects.filter(id=ticket.id).update(**updates)
        await log_activity(
            access.agent.tenant_id,
            ticket.id,
            "agent",
            access.agent.id,
            ActivityAction.UPDATED,
        )

    return Response.redirect(f"/tickets/{ticket.id}")


@app.post("/tickets/{ticket_id:int}/assign")
@guard(
    *REQUIRE_AGENT,
    Require.resource("access", resolver=_resolve_ticket_assign, from_path="ticket_id"),
)
async def ticket_assign_handler(request, ticket_id: int):
    """Assign ticket to agent and/or team. Requires team_lead+."""
    access = request.guard.access
    ticket = access.ticket

    try:
        data = await validate_form(request, TicketAssignSchema)
    except ValidationErrors as exc:
        raise HTTPException(400, str(exc.errors))

    updates: dict[str, object] = {}
    if data.assignee_id:
        assignee = await Agent.objects.filter(id=data.assignee_id).first()
        if not assignee:
            raise HTTPException(400, "Agent not found")
        updates["assignee_id"] = data.assignee_id
    if data.team_id:
        team = await Team.objects.filter(id=data.team_id).first()
        if not team:
            raise HTTPException(400, "Team not found")
        updates["team_id"] = data.team_id

    if updates:
        await Ticket.objects.filter(id=ticket.id).update(**updates)
        await log_activity(
            access.agent.tenant_id,
            ticket.id,
            "agent",
            access.agent.id,
            ActivityAction.ASSIGNED,
            f'{{"assignee_id": {updates.get("assignee_id", ticket.assignee_id)}, "team_id": {updates.get("team_id", ticket.team_id)}}}',
        )

    return Response.redirect(f"/tickets/{ticket.id}")


@app.post("/tickets/{ticket_id:int}/close")
@guard(
    *REQUIRE_AGENT,
    Require.resource("access", resolver=_resolve_ticket_close, from_path="ticket_id"),
)
async def ticket_close_handler(request, ticket_id: int):
    """Close a ticket. Validates status transition is allowed."""
    access = request.guard.access
    ticket = access.ticket

    try:
        data = await validate_form(request, TicketCloseSchema)
    except ValidationErrors as exc:
        raise HTTPException(400, str(exc.errors))

    # Find terminal status
    closed_status = await TicketStatusConfig.objects.filter(is_terminal=True).first()
    if not closed_status:
        raise HTTPException(500, "No terminal status configured")

    # Validate transition exists
    transition = await StatusTransition.objects.filter(
        from_status_id=ticket.status_id, to_status_id=closed_status.id
    ).first()
    if not transition:
        raise HTTPException(400, "Cannot close from current status")

    old_status_id = ticket.status_id
    await Ticket.objects.filter(id=ticket.id).update(status_id=closed_status.id)

    # Optional close comment
    if data.comment:
        await Comment(
            tenant_id=access.agent.tenant_id,
            ticket_id=ticket.id,
            author_type=AuthorType.AGENT,
            author_id=access.agent.id,
            body=data.comment,
            is_internal=True,
        ).save()

    await log_activity(
        access.agent.tenant_id,
        ticket.id,
        "agent",
        access.agent.id,
        ActivityAction.STATUS_CHANGED,
        f'{{"old_status_id": {old_status_id}, "new_status_id": {closed_status.id}}}',
    )

    return Response.redirect(f"/tickets/{ticket.id}")


@app.post("/tickets/{ticket_id:int}/reopen")
@guard(
    *REQUIRE_AGENT,
    Require.resource("access", resolver=_resolve_ticket_reopen, from_path="ticket_id"),
)
async def ticket_reopen_handler(request, ticket_id: int):
    """Reopen a closed ticket."""
    access = request.guard.access
    ticket = access.ticket

    reopened_status = await TicketStatusConfig.objects.filter(slug="reopened").first()
    if not reopened_status:
        reopened_status = await TicketStatusConfig.objects.filter(slug="open").first()
    if not reopened_status:
        raise HTTPException(500, "No reopen status configured")

    # Validate transition
    transition = await StatusTransition.objects.filter(
        from_status_id=ticket.status_id, to_status_id=reopened_status.id
    ).first()
    if not transition:
        raise HTTPException(400, "Cannot reopen from current status")

    await Ticket.objects.filter(id=ticket.id).update(status_id=reopened_status.id)
    await log_activity(
        access.agent.tenant_id,
        ticket.id,
        "agent",
        access.agent.id,
        ActivityAction.REOPENED,
    )

    return Response.redirect(f"/tickets/{ticket.id}")


@app.post("/tickets/{ticket_id:int}/lock")
@guard(
    *REQUIRE_AGENT,
    Require.resource("access", resolver=_resolve_ticket_lock, from_path="ticket_id"),
)
async def ticket_lock_handler(request, ticket_id: int):
    """Toggle lock state. Requires team_lead+."""
    access = request.guard.access
    ticket = access.ticket
    is_locked = await ticket.has_status("state", "locked")
    if is_locked:
        await ticket.clear_status(
            "state", reason="Unlocked by agent", actor_id=access.agent.id
        )
        action = ActivityAction.UNLOCKED
    else:
        await ticket.set_status(
            "state", "locked", reason="Locked by agent", actor_id=access.agent.id
        )
        action = ActivityAction.LOCKED
    await log_activity(
        access.agent.tenant_id, ticket.id, "agent", access.agent.id, action
    )
    return Response.redirect(f"/tickets/{ticket.id}")


@app.post("/tickets/{ticket_id:int}/mute")
@guard(
    *REQUIRE_AGENT,
    Require.resource("access", resolver=_resolve_ticket_update, from_path="ticket_id"),
)
async def ticket_mute_handler(request, ticket_id: int):
    """Toggle mute — suppress notifications for this ticket."""
    access = request.guard.access
    ticket = access.ticket
    is_muted = await ticket.has_status("state", "muted")
    if is_muted:
        await ticket.clear_status(
            "state", reason="Unmuted by agent", actor_id=access.agent.id
        )
    else:
        await ticket.set_status(
            "state", "muted", reason="Muted by agent", actor_id=access.agent.id
        )
    return Response.redirect(f"/tickets/{ticket.id}")


@app.post("/tickets/{ticket_id:int}/merge")
@guard(
    *REQUIRE_AGENT,
    Require.resource("access", resolver=_resolve_ticket_merge, from_path="ticket_id"),
)
async def ticket_merge_handler(request, ticket_id: int):
    """Merge this ticket into a target. Moves comments, closes source."""
    access = request.guard.access
    ticket = access.ticket

    try:
        data = await validate_form(request, TicketMergeSchema)
    except ValidationErrors as exc:
        raise HTTPException(400, str(exc.errors))

    target = await Ticket.objects.filter(id=data.target_ticket_id).first()
    if not target:
        raise HTTPException(400, "Target ticket not found")
    if target.id == ticket.id:
        raise HTTPException(400, "Cannot merge ticket into itself")
    if target.tenant_id != ticket.tenant_id:
        raise HTTPException(400, "Target ticket not found")  # Tenant isolation

    db = get_db()
    async with db.transaction():
        # Move comments to target
        await Comment.objects.filter(ticket_id=ticket.id).update(ticket_id=target.id)

        # Close source
        closed_status = await TicketStatusConfig.objects.filter(
            is_terminal=True
        ).first()
        closed_id = closed_status.id if closed_status else ticket.status_id
        await Ticket.objects.filter(id=ticket.id).update(
            merged_into_id=target.id, status_id=closed_id
        )

    await log_activity(
        access.agent.tenant_id,
        ticket.id,
        "agent",
        access.agent.id,
        ActivityAction.MERGED,
        f'{{"target_ticket_id": {target.id}}}',
    )

    return Response.redirect(f"/tickets/{target.id}")


@app.post("/tickets/{ticket_id:int}/split")
@guard(
    *REQUIRE_AGENT,
    Require.resource("access", resolver=_resolve_ticket_update, from_path="ticket_id"),
)
async def ticket_split_handler(request, ticket_id: int):
    """Split — create a child ticket."""
    access = request.guard.access
    ticket = access.ticket

    try:
        data = await validate_form(request, TicketSplitSchema)
    except ValidationErrors as exc:
        raise HTTPException(400, str(exc.errors))

    tenant_id = access.agent.tenant_id

    default_status = await TicketStatusConfig.objects.filter(is_default=True).first()
    status_id = default_status.id if default_status else ticket.status_id

    child_number = await next_ticket_number(tenant_id)
    child = Ticket(
        tenant_id=tenant_id,
        ticket_number=child_number,
        title=data.title,
        description=data.description,
        status_id=status_id,
        priority_id=ticket.priority_id,
        ticket_type_id=ticket.ticket_type_id,
        customer_id=ticket.customer_id,
        assignee_id=ticket.assignee_id,
        team_id=ticket.team_id,
        parent_ticket_id=ticket.id,
        source=ticket.source,
    )
    await child.save()

    await log_activity(
        access.agent.tenant_id,
        ticket.id,
        "agent",
        access.agent.id,
        ActivityAction.SPLIT,
        f'{{"child_ticket_id": {child.id}, "child_number": "{child_number}"}}',
    )

    return Response.redirect(f"/tickets/{child.id}")


@app.post("/tickets/{ticket_id:int}/comments")
@guard(
    *REQUIRE_AGENT,
    Require.resource("access", resolver=_resolve_ticket_read, from_path="ticket_id"),
)
async def ticket_add_comment(request, ticket_id: int):
    """Add a comment to a ticket."""
    access = request.guard.access
    ticket = access.ticket

    if await ticket.has_status("state", "locked"):
        raise HTTPException(403, "This ticket is locked")

    try:
        data = await validate_form(request, CommentCreateSchema)
    except ValidationErrors as exc:
        raise HTTPException(400, str(exc.errors))

    comment = Comment(
        tenant_id=access.agent.tenant_id,
        ticket_id=ticket.id,
        author_type=AuthorType.AGENT,
        author_id=access.agent.id,
        body=data.body,
        is_internal=data.is_internal == "on",
    )
    await comment.save()

    await log_activity(
        access.agent.tenant_id,
        ticket.id,
        "agent",
        access.agent.id,
        ActivityAction.COMMENT_ADDED,
    )

    if request.headers.get("hx-request"):
        ctx = build_context(request, comment=comment, agent=access.agent)
        return app.render("_partials/comment.html", ctx)
    return Response.redirect(f"/tickets/{ticket.id}")


@app.get("/tickets/{ticket_id:int}/timeline")
@guard(
    *REQUIRE_AGENT,
    Require.resource("access", resolver=_resolve_ticket_read, from_path="ticket_id"),
)
async def ticket_timeline(request, ticket_id: int):
    """Activity log for a ticket — JSON endpoint for HTMX."""
    access = request.guard.access
    activity = (
        await ActivityLog.objects.filter(ticket_id=access.ticket.id)
        .order_by("-id")
        .limit(100)
        .all()
    )

    entries = []
    for a in activity:
        entries.append(
            {
                "id": a.id,
                "actor_type": a.actor_type,
                "actor_id": a.actor_id,
                "action": a.action,
                "detail": a.detail,
                "created_at": str(a.created_at),
            }
        )
    return Response.json({"timeline": entries})


# ---------------------------------------------------------------------------
# Customer portal routes
# ---------------------------------------------------------------------------


@app.get("/portal/")
@guard(*REQUIRE_CUSTOMER)
async def portal_dashboard(request):
    """Customer portal dashboard — own tickets summary."""
    customer = request.guard.customer
    qs = Ticket.objects.filter(customer_id=customer.id).order_by("-id")
    tickets, next_cursor, prev_cursor = await paginate(qs, request, page_size=20)
    statuses = await TicketStatusConfig.objects.all()
    status_map = {s.id: s for s in statuses}

    ctx = await build_themed_context(
        request,
        tickets=tickets,
        status_map=status_map,
        customer=customer,
        next_cursor=next_cursor,
        prev_cursor=prev_cursor,
    )
    return app.render("portal_dashboard.html", ctx)


@app.get("/portal/tickets/")
@guard(*REQUIRE_CUSTOMER)
async def portal_ticket_list(request):
    """Customer's own tickets only."""
    customer = request.guard.customer
    qs = Ticket.objects.filter(customer_id=customer.id).order_by("-id")
    tickets, next_cursor, prev_cursor = await paginate(qs, request, page_size=25)
    statuses = await TicketStatusConfig.objects.all()
    status_map = {s.id: s for s in statuses}

    ctx = build_context(
        request,
        tickets=tickets,
        status_map=status_map,
        next_cursor=next_cursor,
        prev_cursor=prev_cursor,
    )
    return app.render("portal_ticket_list.html", ctx)


@app.get("/portal/tickets/{ticket_id:int}")
@guard(
    *REQUIRE_CUSTOMER,
    Require.resource("ticket", resolver=_resolve_portal_ticket, from_path="ticket_id"),
)
async def portal_ticket_detail(request, ticket_id: int):
    """Ticket detail — own ticket, public comments only (internal hidden)."""
    ticket = request.guard.ticket

    # Only public comments
    comments = (
        await Comment.objects.filter(ticket_id=ticket.id, is_internal=False)
        .order_by("id")
        .limit(200)
        .all()
    )

    status = await TicketStatusConfig.objects.filter(id=ticket.status_id).first()
    priority = await PriorityConfig.objects.filter(id=ticket.priority_id).first()

    is_locked = await ticket.has_status("state", "locked")

    ctx = build_context(
        request,
        ticket=ticket,
        comments=comments,
        status=status,
        priority=priority,
        is_locked=is_locked,
    )
    return app.render("portal_ticket_detail.html", ctx)


@app.get("/portal/tickets/new")
@guard(*REQUIRE_CUSTOMER)
async def portal_ticket_create_form(request):
    """Customer ticket submission form."""
    types = (
        await TicketTypeConfig.objects.filter(is_active=True)
        .order_by("sort_order")
        .all()
    )
    ctx = build_context(request, types=types)
    return app.render("portal_ticket_form.html", ctx)


@app.post("/portal/tickets/new")
@guard(*REQUIRE_CUSTOMER)
async def portal_ticket_create_handler(request):
    """Customer submits a new ticket."""
    customer = request.guard.customer

    try:
        data = await validate_form(request, PortalTicketCreateSchema)
    except ValidationErrors as exc:
        ctx = build_context(request, errors=[str(e) for e in exc.errors])
        return app.render("portal_ticket_form.html", ctx, status=400)

    tenant_id = customer.tenant_id

    default_status = await TicketStatusConfig.objects.filter(is_default=True).first()
    if not default_status:
        raise HTTPException(500, "No default status configured")

    # Validate type exists
    ticket_type = await TicketTypeConfig.objects.filter(id=data.ticket_type_id).first()
    if not ticket_type:
        raise HTTPException(400, "Invalid ticket type")

    # Use type's default priority or org default
    default_priority = await PriorityConfig.objects.filter(is_default=True).first()
    priority_id = ticket_type.default_priority_id or (
        default_priority.id if default_priority else 0
    )

    ticket_number = await next_ticket_number(tenant_id)

    ticket = Ticket(
        tenant_id=tenant_id,
        ticket_number=ticket_number,
        title=data.title,
        description=data.description,
        status_id=default_status.id,
        priority_id=priority_id,
        ticket_type_id=data.ticket_type_id,
        customer_id=customer.id,
        source=TicketSource.PORTAL,
    )
    await ticket.save()
    # Note: ActivityLog CREATED entry handled by post_save signal in signals.py

    return Response.redirect(f"/portal/tickets/{ticket.id}")


@app.post("/portal/tickets/{ticket_id:int}/comment")
@guard(
    *REQUIRE_CUSTOMER,
    Require.resource("ticket", resolver=_resolve_portal_ticket, from_path="ticket_id"),
)
async def portal_add_comment(request, ticket_id: int):
    """Customer adds a public comment (internal notes not allowed)."""
    ticket = request.guard.ticket
    customer = request.guard.customer

    if await ticket.has_status("state", "locked"):
        raise HTTPException(403, "This ticket is locked")

    try:
        data = await validate_form(request, PortalCommentSchema)
    except ValidationErrors as exc:
        raise HTTPException(400, str(exc.errors))

    comment = Comment(
        tenant_id=customer.tenant_id,
        ticket_id=ticket.id,
        author_type=AuthorType.CUSTOMER,
        author_id=customer.id,
        body=data.body,
        is_internal=False,  # Customer comments are always public
    )
    await comment.save()

    await log_activity(
        customer.tenant_id,
        ticket.id,
        "customer",
        customer.id,
        ActivityAction.COMMENT_ADDED,
    )

    return Response.redirect(f"/portal/tickets/{ticket.id}")


@app.post("/portal/tickets/{ticket_id:int}/rate")
@guard(
    *REQUIRE_CUSTOMER,
    Require.resource("ticket", resolver=_resolve_portal_ticket, from_path="ticket_id"),
)
async def portal_rate_ticket(request, ticket_id: int):
    """Customer satisfaction rating (CSAT)."""
    ticket = request.guard.ticket
    customer = request.guard.customer

    try:
        data = await validate_form(request, CSATRatingSchema)
    except ValidationErrors as exc:
        raise HTTPException(400, str(exc.errors))

    # Check if already rated
    existing = await SatisfactionRating.objects.filter(
        ticket_id=ticket.id, customer_id=customer.id
    ).first()
    if existing:
        # Update existing rating
        await SatisfactionRating.objects.filter(id=existing.id).update(
            score=data.score, comment=data.comment
        )
    else:
        await SatisfactionRating(
            tenant_id=customer.tenant_id,
            ticket_id=ticket.id,
            customer_id=customer.id,
            score=data.score,
            comment=data.comment,
        ).save()

    return Response.redirect(f"/portal/tickets/{ticket.id}")


# ---------------------------------------------------------------------------
# Management routes (admin-gated)
# ---------------------------------------------------------------------------


@app.get("/agents/")
@guard(*REQUIRE_ADMIN)
async def agent_list(request):
    """List all agents in the org."""
    qs = Agent.objects.order_by("-id")
    agents, next_cursor, prev_cursor = await paginate(qs, request, page_size=50)
    # Pre-compute active status for template rendering (templates can't call async)
    agent_active: dict[int, bool] = {}
    for a in agents:
        agent_active[a.id] = not await a.has_status("lifecycle", "deactivated")
    ctx = build_context(
        request,
        agents=agents,
        next_cursor=next_cursor,
        prev_cursor=prev_cursor,
        agent_active=agent_active,
    )
    return app.render("agent_list.html", ctx)


@app.get("/agents/new")
@guard(*REQUIRE_ADMIN)
async def agent_create_form(request):
    ctx = build_context(request)
    return app.render("agent_form.html", ctx)


@app.post("/agents/new")
@guard(*REQUIRE_ADMIN)
async def agent_create_handler(request):
    """Create a new agent. Admin only."""
    try:
        data = await validate_form(request, AgentCreateSchema)
    except ValidationErrors as exc:
        ctx = build_context(request, errors=[str(e) for e in exc.errors])
        return app.render("agent_form.html", ctx, status=400)

    admin_agent = request.guard.agent
    tenant_id = admin_agent.tenant_id

    # Check email uniqueness
    existing = await Agent.objects.filter(email=data.email).first()
    if existing:
        ctx = build_context(request, errors=["Email already in use"])
        return app.render("agent_form.html", ctx, status=400)

    agent = Agent(
        tenant_id=tenant_id,
        email=data.email,
        display_name=data.display_name,
        password_hash=hash_password(data.password),
        role=AgentRole(data.role),
    )
    await agent.save()

    return Response.redirect("/agents/")


@app.get("/teams/")
@guard(*REQUIRE_AGENT)
async def team_list(request):
    """List teams."""
    teams = await Team.objects.filter(is_active=True).order_by("name").all()
    ctx = build_context(request, teams=teams)
    return app.render("team_list.html", ctx)


@app.post("/teams/new")
@guard(*REQUIRE_ADMIN)
async def team_create_handler(request):
    """Create a new team. Admin only."""
    try:
        data = await validate_form(request, TeamCreateSchema)
    except ValidationErrors as exc:
        raise HTTPException(400, str(exc.errors))

    team = Team(
        tenant_id=request.guard.agent.tenant_id,
        name=data.name,
        slug=data.slug,
        description=data.description,
        lead_agent_id=data.lead_agent_id,
    )
    await team.save()

    return Response.redirect("/teams/")


@app.get("/tags/")
@guard(*REQUIRE_AGENT)
async def tag_list(request):
    """List tags."""
    tags = await Tag.objects.order_by("name").all()
    ctx = build_context(request, tags=tags)
    return app.render("tag_list.html", ctx)


@app.post("/tags/new")
@guard(*REQUIRE_TEAM_LEAD)
async def tag_create_handler(request):
    """Create a new tag. Team lead+ only."""
    try:
        data = await validate_form(request, TagCreateSchema)
    except ValidationErrors as exc:
        raise HTTPException(400, str(exc.errors))

    tenant_id = request.guard.agent.tenant_id

    # Check uniqueness within tenant
    existing = await Tag.objects.filter(name=data.name).first()
    if existing:
        raise HTTPException(400, "Tag already exists")

    tag = Tag(
        tenant_id=tenant_id,
        name=data.name,
        color=data.color,
        description=data.description,
    )
    await tag.save()

    return Response.json(
        {"id": tag.id, "name": tag.name, "color": tag.color}, status=201
    )


# ---------------------------------------------------------------------------
# Search route
# ---------------------------------------------------------------------------


@app.get("/search/")
@guard(*REQUIRE_AGENT)
async def search_handler(request):
    """Full-text search — renders search page with results."""
    agent = request.guard.agent
    tenant_id = agent.tenant_id
    query = request.GET.get("q", "").strip()

    tickets: list[dict[str, object]] = []
    comments: list[dict[str, object]] = []

    if query:
        adapter_ctx = AdapterContext(
            tenant_id=tenant_id,
            actor_type="agent",
            actor_id=agent.id,
            request=request,
        )
        query = await adapter_registry.run_search_pre(adapter_ctx, query)
        tickets = await search_tickets(query, tenant_id)
        comments = await search_comments(query, tenant_id)
        tickets = await adapter_registry.run_search_post(adapter_ctx, query, tickets)

    ctx = build_context(request, query=query, tickets=tickets, comments=comments)
    return app.render("search.html", ctx)


# ---------------------------------------------------------------------------
# Analytics routes
# ---------------------------------------------------------------------------


@app.get("/dashboard/")
@guard(*REQUIRE_AGENT)
async def analytics_dashboard(request):
    """Dashboard page: open tickets, avg resolution, SLA compliance."""
    agent = request.guard.agent
    tenant_id = agent.tenant_id

    db = get_db()
    row = await db.query_one(
        "SELECT "
        "  COUNT(*) FILTER (WHERE is_deleted = FALSE AND is_current = TRUE) AS total_tickets, "
        "  COUNT(*) FILTER (WHERE is_deleted = FALSE AND is_current = TRUE "
        "    AND status_id IN (SELECT id FROM ht_ticket_status_configs WHERE category = 'open' AND tenant_id = $1)) AS open_tickets, "
        "  COUNT(*) FILTER (WHERE is_deleted = FALSE AND is_current = TRUE AND assignee_id = 0) AS unassigned "
        "FROM ht_tickets WHERE tenant_id = $1",
        tenant_id,
    )

    sla_stats = await SLAInstance.objects.filter(tenant_id=tenant_id).aggregate(
        total_sla=Count("id"),
        breached=Count("id", filter_expr={"breached": True}),
    )
    total_sla = sla_stats.get("total_sla", 0) or 0
    breached = sla_stats.get("breached", 0) or 0
    sla_compliance = (
        round((1 - breached / total_sla) * 100, 1) if total_sla > 0 else 100.0
    )

    stats = {
        "total_tickets": row["total_tickets"] if row else 0,
        "open_tickets": row["open_tickets"] if row else 0,
        "unassigned": row["unassigned"] if row else 0,
        "sla_compliance_pct": sla_compliance,
        "sla_breached": breached,
    }

    ctx = await build_themed_context(request, stats=stats)
    return app.render("dashboard.html", ctx)


@app.get("/analytics/agents/")
@guard(*REQUIRE_AGENT)
async def analytics_agents(request):
    """Agent performance list."""
    agent = request.guard.agent
    db = get_db()

    rows = await db.query(
        "SELECT a.id, a.display_name, a.email, "
        "  COUNT(t.id) AS assigned_tickets, "
        "  COUNT(t.id) FILTER (WHERE t.status_id IN "
        "    (SELECT id FROM ht_ticket_status_configs WHERE is_terminal = TRUE AND tenant_id = $1)) AS resolved "
        "FROM ht_agents a "
        "LEFT JOIN ht_tickets t ON t.assignee_id = a.id AND t.is_deleted = FALSE AND t.is_current = TRUE "
        "WHERE a.tenant_id = $1 "
        "  AND NOT EXISTS ("
        "    SELECT 1 FROM hyper_status_events se "
        "    WHERE se.entity_type = 'agent' AND se.entity_id = a.id "
        "      AND se.category = 'lifecycle' AND se.status = 'deactivated' "
        "      AND se.ended_at IS NULL"
        "  ) "
        "GROUP BY a.id, a.display_name, a.email "
        "ORDER BY assigned_tickets DESC",
        agent.tenant_id,
    )

    agents = [dict(r) for r in rows]
    ctx = build_context(request, agents=agents)
    return app.render("_partials/agent_stats.html", ctx)


@app.get("/analytics/teams/")
@guard(*REQUIRE_AGENT)
async def analytics_teams(request):
    """Team performance list."""
    agent = request.guard.agent
    all_teams = (
        await Team.objects.filter(
            tenant_id=agent.tenant_id,
            is_active=True,
        )
        .order_by("name")
        .all()
    )

    teams = []
    for tm in all_teams:
        queue_depth = await Ticket.objects.filter(
            team_id=tm.id,
            is_deleted=False,
            is_current=True,
        ).count()
        teams.append({"id": tm.id, "name": tm.name, "queue_depth": queue_depth})
    teams.sort(key=lambda t: t["queue_depth"], reverse=True)

    ctx = build_context(request, teams=teams)
    return app.render("_partials/team_stats.html", ctx)


# ---------------------------------------------------------------------------
# Export route
# ---------------------------------------------------------------------------


@app.get("/tickets/export/")
@guard(*REQUIRE_AGENT)
async def ticket_export(request):
    """Export tickets as CSV or JSON."""
    agent = request.guard.agent
    fmt = request.GET.get("format", "csv")

    if fmt == "json":
        data = await export_tickets_json(agent.tenant_id)
        return Response(
            body=data.encode(),
            content_type="application/json",
            headers={"Content-Disposition": "attachment; filename=tickets.json"},
        )

    data = await export_tickets_csv(agent.tenant_id)
    return Response(
        body=data.encode(),
        content_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=tickets.csv"},
    )


# ---------------------------------------------------------------------------
# Org admin routes (admin-gated)
# ---------------------------------------------------------------------------


@app.get("/admin/settings/")
@guard(*REQUIRE_ADMIN)
async def org_settings_view(request):
    """Org settings page with usage stats."""
    settings = await OrgSettings.objects.first()

    ticket_count = await Ticket.objects.count()
    # Count agents that are NOT deactivated (no deactivated status = active)
    all_agents = await Agent.objects.all()
    tl = get_timeline()
    agent_count = 0
    for a in all_agents:
        status = await tl.current_status("agent", a.id, "lifecycle")
        if not status or status.status != "deactivated":
            agent_count += 1

    ctx = build_context(
        request,
        settings=settings,
        usage={"tickets_total": ticket_count, "agents_active": agent_count},
    )
    return app.render("org_settings.html", ctx)


# ---------------------------------------------------------------------------
# File storage for attachments
# ---------------------------------------------------------------------------

_upload_storage = MemoryStorage()
_ALLOWED_EXTENSIONS = frozenset(
    {"pdf", "png", "jpg", "jpeg", "gif", "txt", "csv", "doc", "docx", "xlsx", "zip"}
)
_MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB


def _get_extension(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


# ---------------------------------------------------------------------------
# Attachment routes
# ---------------------------------------------------------------------------


@app.get("/tickets/{ticket_id:int}/attachments/")
@guard(
    *REQUIRE_AGENT,
    Require.resource("access", resolver=_resolve_ticket_read, from_path="ticket_id"),
)
async def attachment_list(request, ticket_id: int):
    """List attachments on a ticket."""
    access = request.guard.access
    attachments = (
        await Attachment.objects.filter(ticket_id=access.ticket.id)
        .order_by("-id")
        .limit(100)
        .all()
    )
    return Response.json(
        {
            "attachments": [
                {
                    "id": a.id,
                    "filename": a.filename,
                    "content_type": a.content_type,
                    "size_bytes": a.size_bytes,
                    "created_at": str(a.created_at),
                }
                for a in attachments
            ]
        }
    )


@app.post("/tickets/{ticket_id:int}/attachments/")
@guard(
    *REQUIRE_AGENT,
    Require.resource("access", resolver=_resolve_ticket_read, from_path="ticket_id"),
)
async def attachment_upload(request, ticket_id: int):
    """Upload a file attachment to a ticket."""
    access = request.guard.access
    ticket = access.ticket

    uploaded_files = await request.files()
    uploaded = uploaded_files.get("file")
    if not uploaded or not uploaded.filename:
        raise HTTPException(400, "No file uploaded")

    original_name = uploaded.filename.split("/")[-1].split("\\")[-1]
    if not original_name:
        original_name = "unnamed"
    content = uploaded.data
    content_type = uploaded.content_type or "application/octet-stream"

    ext = _get_extension(original_name)
    if ext not in _ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Extension '.{ext}' not allowed")

    size = len(content)
    if size > _MAX_FILE_SIZE:
        raise HTTPException(
            400,
            f"File too large ({size // 1024}KB). Max {_MAX_FILE_SIZE // (1024 * 1024)}MB",
        )

    stored_name = await _upload_storage.save(original_name, content)

    attachment = Attachment(
        tenant_id=access.agent.tenant_id,
        ticket_id=ticket.id,
        filename=original_name,
        content_type=content_type,
        size_bytes=size,
        storage_path=stored_name,
        uploaded_by_type=AuthorType.AGENT,
        uploaded_by_id=access.agent.id,
    )
    await attachment.save()

    await log_activity(
        access.agent.tenant_id,
        ticket.id,
        "agent",
        access.agent.id,
        ActivityAction.UPDATED,
        f'{{"attachment_added": "{original_name}"}}',
    )

    if request.headers.get("hx-request"):
        return Response.html(f"<div>Uploaded: {original_name} ({size // 1024}KB)</div>")
    return Response.redirect(f"/tickets/{ticket.id}")


@app.get("/tickets/{ticket_id:int}/attachments/{attachment_id:int}/download")
@guard(
    *REQUIRE_AGENT,
    Require.resource("access", resolver=_resolve_ticket_read, from_path="ticket_id"),
)
async def attachment_download(request, ticket_id: int, attachment_id: int):
    """Download an attachment."""
    attachment = await Attachment.objects.filter(
        id=attachment_id, ticket_id=ticket_id
    ).first()
    if not attachment:
        raise HTTPException(404, "Attachment not found")

    data = await _upload_storage.read(attachment.storage_path)
    if data is None:
        raise HTTPException(404, "File not found in storage")

    return Response(
        body=data,
        content_type=attachment.content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{attachment.filename}"'
        },
    )


# ---------------------------------------------------------------------------
# Ticket template routes
# ---------------------------------------------------------------------------


@app.get("/templates/")
@guard(*REQUIRE_AGENT)
async def template_list(request):
    """List available ticket templates."""
    templates = await TicketTemplate.objects.order_by("name").all()
    ctx = build_context(request, templates=templates)
    return app.render("template_list.html", ctx)


@app.post("/tickets/{ticket_id:int}/apply-template")
@guard(
    *REQUIRE_AGENT,
    Require.resource("access", resolver=_resolve_ticket_update, from_path="ticket_id"),
)
async def apply_template_handler(request, ticket_id: int):
    """Apply a template's fields to a ticket."""
    access = request.guard.access
    ticket = access.ticket

    try:
        data = await validate_form(request, ApplyTemplateSchema)
    except ValidationErrors as exc:
        raise HTTPException(400, str(exc.errors))

    template = await TicketTemplate.objects.filter(id=data.template_id).first()
    if not template:
        raise HTTPException(404, "Template not found")

    updates: dict[str, object] = {}
    if template.default_title:
        updates["title"] = template.default_title
    if template.default_body:
        updates["description"] = template.default_body
    if template.ticket_type_id:
        updates["ticket_type_id"] = template.ticket_type_id
    if template.priority_id:
        updates["priority_id"] = template.priority_id
    if template.default_team_id:
        updates["team_id"] = template.default_team_id

    if updates:
        await Ticket.objects.filter(id=ticket.id).update(**updates)

    return Response.redirect(f"/tickets/{ticket.id}")


# ---------------------------------------------------------------------------
# Canned response routes
# ---------------------------------------------------------------------------


@app.get("/canned-responses/")
@guard(*REQUIRE_AGENT)
async def canned_response_list(request):
    """List canned responses."""
    responses = await CannedResponse.objects.order_by("title").all()
    ctx = build_context(request, canned_responses=responses)
    return app.render("canned_response_list.html", ctx)


@app.post("/tickets/{ticket_id:int}/apply-canned/{canned_id:int}")
@guard(
    *REQUIRE_AGENT,
    Require.resource("access", resolver=_resolve_ticket_read, from_path="ticket_id"),
)
async def apply_canned_response(request, ticket_id: int, canned_id: int):
    """Apply a canned response as a comment on a ticket."""
    access = request.guard.access
    ticket = access.ticket

    canned = await CannedResponse.objects.filter(id=canned_id).first()
    if not canned:
        raise HTTPException(404, "Canned response not found")

    # Variable substitution
    customer = await Customer.objects.filter(id=ticket.customer_id).first()
    body = canned.body
    body = body.replace("{{ticket_number}}", ticket.ticket_number)
    body = body.replace(
        "{{customer_name}}", customer.display_name if customer else "Customer"
    )
    body = body.replace("{{agent_name}}", access.agent.display_name)

    comment = Comment(
        tenant_id=access.agent.tenant_id,
        ticket_id=ticket.id,
        author_type=AuthorType.AGENT,
        author_id=access.agent.id,
        body=body,
        body_html=canned.body_html or "",
    )
    await comment.save()

    await log_activity(
        access.agent.tenant_id,
        ticket.id,
        "agent",
        access.agent.id,
        ActivityAction.COMMENT_ADDED,
        f'{{"canned_response_id": {canned.id}}}',
    )

    return Response.redirect(f"/tickets/{ticket.id}")


# ---------------------------------------------------------------------------
# Saved views routes
# ---------------------------------------------------------------------------


@app.get("/saved-views/")
@guard(*REQUIRE_AGENT)
async def saved_view_list(request):
    """List agent's saved views + shared views."""
    agent = request.guard.agent
    # Own views + shared (agent_id=0)
    views = (
        await SavedView.objects.filter(agent_id__in=[agent.id, 0])
        .order_by("name")
        .all()
    )
    ctx = build_context(request, saved_views=views)
    return app.render("saved_view_list.html", ctx)


class SavedViewCreateSchema(ValidatedModel):
    name: str = VField(min_length=1, max_length=100, strip_whitespace=True)
    filter_criteria: str = VField(default="{}", max_length=5000)
    sort_order: str = VField(default="-created_at", max_length=100)
    is_shared: str = VField(default="")  # "on" = shared


@app.post("/saved-views/new")
@guard(*REQUIRE_AGENT)
async def saved_view_create(request):
    """Save current filter criteria as a named view."""
    agent = request.guard.agent

    try:
        data = await validate_form(request, SavedViewCreateSchema)
    except ValidationErrors as exc:
        raise HTTPException(400, str(exc.errors))

    view = SavedView(
        tenant_id=agent.tenant_id,
        name=data.name,
        agent_id=0 if data.is_shared == "on" else agent.id,
        filter_criteria=data.filter_criteria,
        sort_order=data.sort_order,
    )
    await view.save()

    return Response.redirect("/saved-views/")


# ---------------------------------------------------------------------------
# Kanban board + ticket relations
# ---------------------------------------------------------------------------


@app.get("/board/")
@guard(*REQUIRE_AGENT)
async def kanban_board(request):
    """Kanban board — tickets grouped by status columns."""
    agent = request.guard.agent
    tenant_id = agent.tenant_id

    db = get_db()
    statuses = (
        await TicketStatusConfig.objects.filter(is_active=True)
        .order_by("sort_order")
        .all()
    )
    status_ids = [s.id for s in statuses]

    # Single query for all tickets across all active statuses
    all_tickets = (
        await Ticket.objects.filter(
            status_id__in=status_ids,
        )
        .order_by("-id")
        .limit(500)
        .all()
    )

    # Group by status in Python (cap 50 per column)
    tickets_by_status: dict[int, list[object]] = {sid: [] for sid in status_ids}
    for t in all_tickets:
        bucket = tickets_by_status.get(t.status_id)
        if bucket is not None and len(bucket) < 50:
            bucket.append(t)

    columns: list[dict[str, object]] = []
    for status in statuses:
        bucket = tickets_by_status.get(status.id, [])
        columns.append(
            {
                "status": status,
                "tickets": bucket,
                "count": len(bucket),
            }
        )

    ctx = build_context(request, columns=columns)
    return app.render("board.html", ctx)


class TicketRelationSchema(ValidatedModel):
    target_ticket_id: int = VField(ge=1)
    relation_type: str = VField(
        default="related", pattern=r"^(related|blocks|blocked_by|duplicates)$"
    )


@app.post("/tickets/{ticket_id:int}/relate")
@guard(
    *REQUIRE_AGENT,
    Require.resource("access", resolver=_resolve_ticket_update, from_path="ticket_id"),
)
async def add_ticket_relation(request, ticket_id: int):
    """Add a relation between two tickets."""
    access = request.guard.access

    try:
        data = await validate_form(request, TicketRelationSchema)
    except ValidationErrors as exc:
        raise HTTPException(400, str(exc.errors))

    target = await Ticket.objects.filter(id=data.target_ticket_id).first()
    if not target or target.tenant_id != access.agent.tenant_id:
        raise HTTPException(404, "Target ticket not found")

    await TicketRelation(
        tenant_id=access.agent.tenant_id,
        source_ticket_id=access.ticket.id,
        target_ticket_id=data.target_ticket_id,
        relation_type=RelationType(data.relation_type),
    ).save()

    return Response.redirect(f"/tickets/{access.ticket.id}")


# ---------------------------------------------------------------------------
# Bulk operations
# ---------------------------------------------------------------------------


class BulkUpdateSchema(ValidatedModel):
    ticket_ids: str = VField(min_length=1)  # comma-separated IDs
    action: str = VField(pattern=r"^(assign|change_priority|change_status|close)$")
    value: int = VField(default=0, ge=0)  # agent_id, priority_id, or status_id


@app.post("/tickets/bulk-update")
@guard(*REQUIRE_TEAM_LEAD)
async def bulk_update_handler(request):
    """Bulk update tickets — requires team_lead+."""
    agent = request.guard.agent

    try:
        data = await validate_form(request, BulkUpdateSchema)
    except ValidationErrors as exc:
        raise HTTPException(400, str(exc.errors))

    ids = [int(x.strip()) for x in data.ticket_ids.split(",") if x.strip().isdigit()]
    if not ids:
        raise HTTPException(400, "No valid ticket IDs")

    if data.action == "assign":
        await Ticket.objects.filter(id__in=ids).update(assignee_id=data.value)
    elif data.action == "change_priority":
        await Ticket.objects.filter(id__in=ids).update(priority_id=data.value)
    elif data.action == "change_status":
        await Ticket.objects.filter(id__in=ids).update(status_id=data.value)
    elif data.action == "close":
        closed = await TicketStatusConfig.objects.filter(is_terminal=True).first()
        if closed:
            await Ticket.objects.filter(id__in=ids).update(status_id=closed.id)

    for tid in ids:
        await log_activity(
            agent.tenant_id,
            tid,
            "agent",
            agent.id,
            ActivityAction.UPDATED,
            f'{{"bulk_action": "{data.action}"}}',
        )

    return Response.redirect("/tickets/")


# ---------------------------------------------------------------------------
# Approval workflow routes
# ---------------------------------------------------------------------------


class ApprovalRequestSchema(ValidatedModel):
    comment: str = VField(default="", max_length=2000, strip_whitespace=True)


@app.post("/tickets/{ticket_id:int}/request-approval")
@guard(
    *REQUIRE_AGENT,
    Require.resource("access", resolver=_resolve_ticket_read, from_path="ticket_id"),
)
async def request_approval(request, ticket_id: int):
    """Request approval before closing a ticket."""
    access = request.guard.access

    try:
        data = await validate_form(request, ApprovalRequestSchema)
    except ValidationErrors as exc:
        raise HTTPException(400, str(exc.errors))

    approval = Approval(
        tenant_id=access.agent.tenant_id,
        ticket_id=access.ticket.id,
        requested_by_id=access.agent.id,
        comment=data.comment,
    )
    await approval.save()

    return Response.redirect(f"/tickets/{access.ticket.id}")


@app.get("/approvals/")
@guard(*REQUIRE_TEAM_LEAD)
async def approval_list(request):
    """Pending approvals for team lead+."""
    qs = Approval.objects.filter(
        status=ApprovalStatus.PENDING.value,
    ).order_by("-id")
    approvals, next_cursor, prev_cursor = await paginate(qs, request, page_size=25)

    # Batch fetch ticket numbers
    ticket_ids = [a.ticket_id for a in approvals]
    tickets = await Ticket.objects.filter(id__in=ticket_ids).all() if ticket_ids else []
    ticket_map = {t.id: t for t in tickets}

    ctx = build_context(
        request,
        approvals=approvals,
        ticket_map=ticket_map,
        next_cursor=next_cursor,
        prev_cursor=prev_cursor,
    )
    return app.render("approval_list.html", ctx)


@app.post("/approvals/{approval_id:int}/approve")
@guard(*REQUIRE_TEAM_LEAD)
async def approve_handler(request, approval_id: int):
    """Approve a pending approval."""
    agent = request.guard.agent
    approval = await Approval.objects.filter(id=approval_id).first()
    if not approval or approval.status != ApprovalStatus.PENDING.value:
        raise HTTPException(404, "Approval not found or already decided")

    await Approval.objects.filter(id=approval.id).update(
        status=ApprovalStatus.APPROVED.value,
        approver_id=agent.id,
    )

    # Complete the blocked status transition (close the ticket)
    closed = await TicketStatusConfig.objects.filter(is_terminal=True).first()
    if closed:
        await Ticket.objects.filter(id=approval.ticket_id).update(status_id=closed.id)

    return Response.redirect("/approvals/")


@app.post("/approvals/{approval_id:int}/reject")
@guard(*REQUIRE_TEAM_LEAD)
async def reject_handler(request, approval_id: int):
    """Reject a pending approval."""
    agent = request.guard.agent
    approval = await Approval.objects.filter(id=approval_id).first()
    if not approval or approval.status != ApprovalStatus.PENDING.value:
        raise HTTPException(404, "Approval not found or already decided")

    await Approval.objects.filter(id=approval.id).update(
        status=ApprovalStatus.REJECTED.value,
        approver_id=agent.id,
    )

    return Response.redirect("/approvals/")


# ---------------------------------------------------------------------------
# @mention parsing (wired into comment create)
# ---------------------------------------------------------------------------

_MENTION_PATTERN = re.compile(r"@(\w+)")


async def parse_and_notify_mentions(
    comment_body: str,
    ticket_id: int,
    actor_id: int,
    tenant_id: int,
) -> list[int]:
    """Parse @mentions from comment body, return mentioned agent IDs, send notifications."""
    matches = _MENTION_PATTERN.findall(comment_body)
    if not matches:
        return []

    unique_names = list(set(matches))

    # Batch fetch agents by display name (single query)
    agents_by_name = await Agent.objects.filter(display_name__in=unique_names).all()
    name_to_agent: dict[str, object] = {a.display_name: a for a in agents_by_name}

    mentioned_ids: list[int] = []
    for name in unique_names:
        agent = name_to_agent.get(name)
        if agent and agent.id != actor_id:
            mentioned_ids.append(agent.id)
            await broadcast_notification(
                tenant_id=tenant_id,
                user_id=agent.id,
                notification_type="mention",
                message=f"You were mentioned in a comment on ticket #{ticket_id}",
                ticket_id=ticket_id,
            )

    return mentioned_ids


# ---------------------------------------------------------------------------
# API key management
# ---------------------------------------------------------------------------


@app.get("/admin/api-keys/")
@guard(*REQUIRE_ADMIN)
async def api_key_list(request):
    """List org's API keys (prefix only)."""
    keys = await OrgAPIKey.objects.order_by("-id").all()
    ctx = build_context(request, api_keys=keys)
    return app.render("api_key_list.html", ctx)


class APIKeyCreateSchema(ValidatedModel):
    name: str = VField(min_length=1, max_length=100, strip_whitespace=True)


@app.post("/admin/api-keys/new")
@guard(*REQUIRE_ADMIN)
async def api_key_create(request):
    """Generate a new API key. Shows full key ONCE."""
    agent = request.guard.agent

    try:
        data = await validate_form(request, APIKeyCreateSchema)
    except ValidationErrors as exc:
        raise HTTPException(400, str(exc.errors))

    # Generate signed API key via SignedAPIKeyMixin
    result = await OrgAPIKey.generate(
        tenant_id=agent.tenant_id,
        name=data.name,
    )

    # Show the full key ONCE
    ctx = build_context(request, new_key=result.raw_key, key_name=data.name)
    return app.render("api_key_created.html", ctx)


@app.post("/admin/api-keys/{key_id:int}/revoke")
@guard(*REQUIRE_ADMIN)
async def api_key_revoke(request, key_id: int):
    """Revoke an API key."""
    await OrgAPIKey.objects.filter(id=key_id).update(is_active=False)
    return Response.redirect("/admin/api-keys/")


# ---------------------------------------------------------------------------
# Tag management on tickets
# ---------------------------------------------------------------------------


@app.post("/tickets/{ticket_id:int}/tags/add")
@guard(
    *REQUIRE_AGENT,
    Require.resource("access", resolver=_resolve_ticket_update, from_path="ticket_id"),
)
async def add_tag_to_ticket(request, ticket_id: int):
    """Add a tag to a ticket."""
    access = request.guard.access

    try:
        data = await validate_form(request, TagApplySchema)
    except ValidationErrors as exc:
        raise HTTPException(400, str(exc.errors))

    tag = await Tag.objects.filter(id=data.tag_id).first()
    if not tag:
        raise HTTPException(404, "Tag not found")

    existing = await TicketTag.objects.filter(
        ticket_id=access.ticket.id, tag_id=tag.id
    ).first()
    if not existing:
        await TicketTag(
            tenant_id=access.agent.tenant_id,
            ticket_id=access.ticket.id,
            tag_id=tag.id,
        ).save()

    await log_activity(
        access.agent.tenant_id,
        access.ticket.id,
        "agent",
        access.agent.id,
        ActivityAction.TAG_ADDED,
        f'{{"tag_id": {tag.id}, "tag_name": "{tag.name}"}}',
    )

    return Response.redirect(f"/tickets/{access.ticket.id}")


@app.post("/tickets/{ticket_id:int}/tags/{tag_id:int}/remove")
@guard(
    *REQUIRE_AGENT,
    Require.resource("access", resolver=_resolve_ticket_update, from_path="ticket_id"),
)
async def remove_tag_from_ticket(request, ticket_id: int, tag_id: int):
    """Remove a tag from a ticket."""
    access = request.guard.access

    await TicketTag.objects.filter(
        ticket_id=access.ticket.id,
        tag_id=tag_id,
    ).delete()

    await log_activity(
        access.agent.tenant_id,
        access.ticket.id,
        "agent",
        access.agent.id,
        ActivityAction.TAG_REMOVED,
        f'{{"tag_id": {tag_id}}}',
    )

    return Response.redirect(f"/tickets/{access.ticket.id}")


# ---------------------------------------------------------------------------
# Volume trends
# ---------------------------------------------------------------------------


@app.get("/analytics/volume/")
@guard(*REQUIRE_AGENT)
async def volume_trends(request):
    """Ticket creation volume by day — HTMX partial for dashboard."""
    agent = request.guard.agent
    db = get_db()

    rows = await db.query(
        "SELECT DATE(created_at) AS day, COUNT(*) AS count "
        "FROM ht_tickets "
        "WHERE tenant_id = $1 AND is_deleted = FALSE AND is_current = TRUE "
        "GROUP BY DATE(created_at) "
        "ORDER BY day DESC LIMIT 30",
        agent.tenant_id,
    )

    days = [{"day": str(r["day"]), "count": r["count"]} for r in rows]
    ctx = build_context(request, days=days)
    return app.render("_partials/volume_trends.html", ctx)


# ---------------------------------------------------------------------------
# Custom fields schema management
# ---------------------------------------------------------------------------


class CustomFieldsSchema(ValidatedModel):
    schema_json: str = VField(default="[]", max_length=10000, strip_whitespace=True)


@app.get("/admin/custom-fields/")
@guard(*REQUIRE_ADMIN)
async def custom_fields_view(request):
    """View and edit custom field schema."""
    settings = await OrgSettings.objects.first()
    schema_str = settings.custom_fields_schema if settings else "[]"
    try:
        fields = json.loads(schema_str)
    except json.JSONDecodeError, TypeError:
        fields = []
    ctx = build_context(request, custom_fields=fields, schema_json=schema_str)
    return app.render("custom_fields.html", ctx)


@app.post("/admin/custom-fields/")
@guard(*REQUIRE_ADMIN)
async def custom_fields_update(request):
    """Update custom fields schema."""
    try:
        data = await validate_form(request, CustomFieldsSchema)
    except ValidationErrors as exc:
        raise HTTPException(400, str(exc.errors))

    # Validate it's valid JSON array
    try:
        parsed = json.loads(data.schema_json)
        if not isinstance(parsed, list):
            raise HTTPException(400, "Schema must be a JSON array")
    except json.JSONDecodeError as exc:
        raise HTTPException(400, f"Invalid JSON: {exc}")

    settings = await OrgSettings.objects.first()
    if settings:
        await OrgSettings.objects.filter(id=settings.id).update(
            custom_fields_schema=data.schema_json
        )
    return Response.redirect("/admin/custom-fields/")
