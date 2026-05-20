"""
Multi-tenant and hierarchical ownership — zero-intrusion architecture.

Opt-in via TenantMixin: adds tenant_id field + auto-scoped QuerySet.
No changes needed for non-tenant apps. All queries auto-filtered when
tenant context is active (set by TenantMiddleware).

Usage:
    # models.py — one mixin
    from hyperdjango.tenancy import TenantMixin

    class Post(TenantMixin, Model):
        class Meta:
            table = "posts"
        title: str = Field()

    # app.py — one middleware
    from hyperdjango.tenancy import TenantMiddleware, resolve_from_user
    app.use(TenantMiddleware(resolve_tenant=resolve_from_user))

    # That's it. All Post queries auto-scoped to current tenant.
    # Admin auto-filtered. tenant_id auto-set on save.

    # Escape hatch for cross-tenant queries:
    all_posts = await Post.objects.unscoped().all()

    # Background tasks with explicit tenant:
    from hyperdjango.tenancy import tenant_context
    with tenant_context(tenant_id=42):
        posts = await Post.objects.all()  # scoped to tenant 42
"""

import contextvars
import inspect
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass

from hyperdjango.conf import get_setting
from hyperdjango.exceptions import HTTPException
from hyperdjango.logging import logger
from hyperdjango.mixins import TimestampMixin
from hyperdjango.models import Field, Model
from hyperdjango.query import QuerySet
from hyperdjango.signals import pre_save
from hyperdjango.validation.core.fields import FieldInfo
from hyperdjango.where import WhereNode

# Subdomains that are never tenant slugs
_RESERVED_SUBDOMAINS = frozenset({"www", "api", "admin"})


class TenantResolutionError(Exception):
    """Raised when tenant resolution fails or is ambiguous.

    A resolution *failure* is NOT the same as "this request has no tenant".
    Resolvers return ``None`` to mean "explicitly public — no tenant scoping
    applies" (the zero-intrusion guarantee). They raise
    ``TenantResolutionError`` when the request *claimed* a tenant that could
    not be resolved (e.g. an unparseable ``X-Tenant-ID`` header).

    The middleware treats this as a fail-CLOSED signal: the request is denied
    rather than silently run with NO tenant filter — which would otherwise
    leak data across tenants.
    """


# ── Tenant Context (request-scoped via contextvars) ───────────────────────


@dataclass(slots=True, frozen=True)
class TenantRef:
    """Immutable reference to the active tenant for the current request.

    Frozen dataclass — cannot be mutated after creation.
    Stored in a ContextVar — async-safe, zero overhead when not set.
    """

    tenant_id: int
    hierarchy: tuple[int, ...] = ()  # ancestor chain for hierarchical tenancy
    tenant_type: str = ""  # optional label: "org", "team", "project"


_current_tenant: contextvars.ContextVar[TenantRef | None] = contextvars.ContextVar(
    "hyper_current_tenant", default=None
)


def set_tenant(
    tenant_id: int,
    hierarchy: tuple[int, ...] | list[int] | None = None,
    tenant_type: str = "",
) -> contextvars.Token:
    """Set the current tenant for this async context. Returns a reset token."""
    ref = TenantRef(
        tenant_id=tenant_id,
        hierarchy=tuple(hierarchy) if hierarchy else (),
        tenant_type=tenant_type,
    )
    return _current_tenant.set(ref)


def get_tenant() -> TenantRef | None:
    """Get the current tenant, or None if no tenant context is active."""
    return _current_tenant.get()


def clear_tenant() -> None:
    """Clear the current tenant context."""
    _current_tenant.set(None)


@contextmanager
def tenant_context(
    tenant_id: int,
    hierarchy: tuple[int, ...] | list[int] | None = None,
    tenant_type: str = "",
):
    """Context manager for scoped tenant activation.

    Useful in tests, background tasks, and management commands:
        with tenant_context(tenant_id=42):
            posts = await Post.objects.all()  # scoped to tenant 42
    """
    token = set_tenant(tenant_id, hierarchy, tenant_type)
    try:
        yield get_tenant()
    finally:
        _current_tenant.reset(token)


# ── TenantQuerySet (auto-filters by tenant_id) ───────────────────────────


class TenantQuerySet(QuerySet):
    """QuerySet that auto-injects WHERE tenant_id = $N from context.

    Mirrors SoftDeleteQuerySet pattern exactly:
    - _build_where_tree() adds tenant condition when context is active
    - _clone() propagates _unscoped state
    - .unscoped() bypasses filtering for cross-tenant queries

    Fail CLOSED (TENANT_STRICT, default True): when no tenant context is active
    and the query is not .unscoped(), a never-match condition is injected so the
    query returns ZERO rows instead of every tenant's data. Cross-tenant access
    (CLI, migrations, admin, background jobs) must be explicit via .unscoped().
    Set TENANT_STRICT=False for fail-open "no context → global" behaviour,
    intended only for non-security multi-tenant apps.
    """

    def __init__(self, *args, unscoped: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self._unscoped = unscoped

    def _clone(self, **kwargs):
        qs = super()._clone(**kwargs)
        qs._unscoped = self._unscoped
        return qs

    def unscoped(self):
        """Bypass tenant filtering for cross-tenant queries.

        Usage:
            all_posts = await Post.objects.unscoped().all()
        """
        qs = self._clone()
        qs._unscoped = True
        return qs

    def _tenant_scope(self):
        """Resolve how this query is scoped: 'unscoped' | ('scoped', tenant) | 'empty'.

        'empty' (TENANT_STRICT + no active tenant) fails closed — the caller
        injects a never-match condition so no cross-tenant rows can leak.
        """
        if self._unscoped:
            return "unscoped"
        tenant = get_tenant()
        if tenant is not None:
            return ("scoped", tenant)
        # A tenant-scoped model queried with NO active tenant. Fail closed unless
        # the app explicitly opted out of strict tenancy.
        return "empty" if get_setting("TENANT_STRICT") else "unscoped"

    def _build_where_tree(self, table_alias=None, join_aliases=None):
        """Inject tenant_id filter (or a never-match, when strict + no context)."""
        root = super()._build_where_tree(table_alias, join_aliases)
        scope = self._tenant_scope()
        if isinstance(scope, tuple):  # ('scoped', tenant)
            col = f"{table_alias}.tenant_id" if table_alias else "tenant_id"
            root.children.append(
                WhereNode(template=f"{col} = {{}}", bind_values=[scope[1].tenant_id])
            )
        elif scope == "empty":
            # Fail closed: no active tenant → match nothing (no param needed).
            root.children.append(WhereNode(template="1 = 0", bind_values=[]))
        return root

    def _mixin_cache_key(self):
        scope = self._tenant_scope()
        # Distinguish scoped-by-id / unscoped / strict-empty so results never
        # cross-contaminate in the query cache.
        key = ("scoped", scope[1].tenant_id) if isinstance(scope, tuple) else scope
        return ("t", key) + super()._mixin_cache_key()

    def _collect_mixin_params(self, params):
        super()._collect_mixin_params(params)
        scope = self._tenant_scope()
        if isinstance(scope, tuple):  # ('scoped', tenant) — 'empty' adds no param
            params.append(scope[1].tenant_id)


# ── TenantMixin (one-line model opt-in) ───────────────────────────────────


class TenantMixin(Model):
    """Add multi-tenant isolation to a model. One mixin, fully automatic.

    Adds a tenant_id field and auto-scoped QuerySet. All queries through
    Model.objects automatically filter by the current tenant when a tenant
    context is active (set by TenantMiddleware or tenant_context()).

    Usage:
        class Post(TenantMixin, Model):
            class Meta:
                table = "posts"
            title: str = Field()

        # All queries auto-scoped:
        posts = await Post.objects.filter(status="published").all()
        # SQL: SELECT ... FROM posts WHERE tenant_id = $1 AND status = $2

        # Escape hatch:
        all_posts = await Post.objects.unscoped().all()

    Composes with other mixins via MRO:
        class Post(TenantMixin, SoftDeleteMixin, TimestampMixin, Model):
            # Gets: tenant filtering + soft delete + timestamps
    """

    class Meta:
        abstract = True

    _queryset_class = TenantQuerySet

    tenant_id: int = Field(index=True)


class HierarchicalTenantMixin(TenantMixin):
    """TenantMixin with parent reference for org→team→project hierarchies.

    Usage:
        class Team(HierarchicalTenantMixin, Model):
            class Meta:
                table = "teams"
            name: str = Field()
            # parent_tenant_id points to the parent org's tenant
    """

    class Meta:
        abstract = True

    parent_tenant_id: int | None = Field(default=None, index=True)


# ── Tenant Model (optional — for apps that want a tenant table) ───────────

CREATE_TENANTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS hyper_tenants (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    parent_id INTEGER REFERENCES hyper_tenants(id) ON DELETE SET NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    settings JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_tenants_slug ON hyper_tenants(slug);
CREATE INDEX IF NOT EXISTS idx_tenants_parent ON hyper_tenants(parent_id);
"""


class Tenant(TimestampMixin, Model):
    """Optional tenant model for apps that need a tenant table.

    Supports hierarchical tenancy via parent_id (org → team → project).
    """

    class Meta:
        table = "hyper_tenants"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field()
    slug: str = Field()
    parent_id: int | None = Field(default=None, foreign_key="Tenant")
    is_active: bool = Field(default=True)
    settings: str = Field(default="{}")


async def get_tenant_hierarchy(db, tenant_id: int) -> list[int]:
    """Get the ancestor chain for a tenant using a recursive CTE.

    Returns ``[tenant_id, parent_id, grandparent_id, ...]`` ordered
    deterministically from leaf to root.

    A ``depth`` column is carried through the recursion (0 = the tenant
    itself, incrementing toward the root) and the result is ordered by it.
    Without an explicit ``ORDER BY`` the database is free to return the rows
    in any order — callers that rely on ``hierarchy[0]`` being the leaf (or
    that compare chains for equality) would see non-deterministic results.

    The recursive UNION ALL + self-join and the ``ORDER BY depth`` on a
    CTE-only column cannot be cleanly expressed through the ORM, so this
    uses a single parameterised raw query.
    """
    rows = await db.query(
        "WITH RECURSIVE ancestors AS ("
        "  SELECT id, parent_id, 0 AS depth FROM hyper_tenants WHERE id = $1"
        "  UNION ALL "
        "  SELECT t.id, t.parent_id, a.depth + 1 "
        "  FROM hyper_tenants t JOIN ancestors a ON t.id = a.parent_id"
        ") SELECT id FROM ancestors ORDER BY depth",
        tenant_id,
    )
    return [r["id"] for r in rows]


# ── TenantMiddleware (pluggable tenant resolution) ────────────────────────


@dataclass
class TenantMiddleware:
    """Middleware that sets the tenant context for each request.

    Usage:
        app.use(TenantMiddleware(resolve_tenant=resolve_from_user))

    The resolve_tenant callable extracts the tenant from the request.
    Built-in resolvers: resolve_from_user, resolve_from_header,
    resolve_from_subdomain, resolve_from_url.
    """

    resolve_tenant: Callable  # (request) -> TenantRef | None

    def __post_init__(self):
        # Cache async check once at init, not on every request
        self._is_async_resolver = inspect.iscoroutinefunction(self.resolve_tenant)

    async def __call__(self, request, call_next):
        # FAIL CLOSED: a resolver failure must NEVER degrade to "no tenant
        # filter" (which would expose every tenant's rows). We only proceed
        # unscoped when the resolver *explicitly* returns None (public).
        try:
            if self._is_async_resolver:
                tenant_ref = await self.resolve_tenant(request)
            else:
                tenant_ref = self.resolve_tenant(request)
        except TenantResolutionError as exc:
            logger.warning(
                "Tenant resolution failed for {path}: {err} — denying request",
                path=request.path,
                err=str(exc),
            )
            raise HTTPException(403, "Tenant resolution failed") from exc

        # A resolver must return TenantRef (scoped) or None (explicitly
        # public). Anything else is a programming error / ambiguity — deny
        # rather than silently disabling tenant filtering.
        if tenant_ref is not None and not isinstance(tenant_ref, TenantRef):
            logger.error(
                "Tenant resolver returned {typ}, expected TenantRef|None "
                "— denying request to avoid unfiltered cross-tenant access",
                typ=type(tenant_ref).__name__,
            )
            raise HTTPException(403, "Tenant resolution failed")

        token = _current_tenant.set(tenant_ref)
        try:
            return await call_next(request)
        finally:
            _current_tenant.reset(token)


# ── Built-in Tenant Resolvers ─────────────────────────────────────────────


def resolve_from_user(request) -> TenantRef | None:
    """Resolve tenant from request.user.tenant_id (most common).

    Requires SessionAuth to run first so request.user is populated.
    """
    # request.user is typed as Any on Request (set by SessionAuth).
    # tenant_id only exists on User models that use TenantMixin — must check
    # with hasattr since the user model class is not known at compile time.
    user = request.user
    if user is None:
        return None
    if not hasattr(user, "tenant_id"):  # user model may not have tenant_id
        return None
    tid = user.tenant_id
    if tid is None:
        return None
    return TenantRef(tenant_id=tid)


def resolve_from_header(request) -> TenantRef | None:
    """Resolve tenant from the X-Tenant-ID header.

    Returns None when the header is absent (no tenant claimed → public).
    Raises :class:`TenantResolutionError` when the header IS present but
    malformed — a claimed-but-unresolvable tenant must fail closed, never
    silently fall through to an unfiltered query.
    """
    headers = request.headers
    tenant_id_str = headers.get("x-tenant-id", "")
    if not tenant_id_str:
        return None
    try:
        return TenantRef(tenant_id=int(tenant_id_str))
    except (ValueError, TypeError) as exc:
        raise TenantResolutionError(
            f"Unparseable X-Tenant-ID header: {tenant_id_str!r}"
        ) from exc


def make_subdomain_resolver(lookup_tenant: Callable) -> Callable:
    """Create a subdomain-based tenant resolver with DB lookup.

    The lookup_tenant callable maps a slug to a tenant_id:
        async def lookup(slug: str) -> int | None:
            row = await db.query_one("SELECT id FROM hyper_tenants WHERE slug = $1", slug)
            return row["id"] if row else None

    Usage:
        resolver = make_subdomain_resolver(lookup)
        app.use(TenantMiddleware(resolve_tenant=resolver))
    """

    async def resolve(request) -> TenantRef | None:
        host = request.headers.get("host", "")
        if not host:
            return None
        host = host.split(":")[0]  # Strip port
        parts = host.split(".")
        if len(parts) < 3:
            return None
        slug = parts[0]
        if slug in _RESERVED_SUBDOMAINS:
            return None
        if inspect.iscoroutinefunction(lookup_tenant):
            tenant_id = await lookup_tenant(slug)
        else:
            tenant_id = lookup_tenant(slug)
        if tenant_id is None:
            return None
        return TenantRef(tenant_id=tenant_id, tenant_type=slug)

    return resolve


def resolve_from_url(request) -> TenantRef | None:
    """Resolve tenant from the URL path prefix ``/t/{tenant_id}/...``.

    Returns None when the path has no ``/t/`` prefix (no tenant claimed →
    public). Raises :class:`TenantResolutionError` when the prefix IS present
    but the id segment is malformed — fail closed rather than run unfiltered.
    """
    path = request.path
    if not path.startswith("/t/"):
        return None
    # Extract tenant_id from /t/{id}/...
    parts = path.split("/")
    if len(parts) < 3 or not parts[2]:
        raise TenantResolutionError(f"Malformed tenant URL prefix: {path!r}")
    try:
        return TenantRef(tenant_id=int(parts[2]))
    except (ValueError, TypeError) as exc:
        raise TenantResolutionError(
            f"Unparseable tenant id in URL: {parts[2]!r}"
        ) from exc


# ── Signal Integration (auto-populate tenant_id on save) ──────────────────


@pre_save.connect
async def _auto_set_tenant(sender, instance, created, **kwargs):
    """Enforce tenant ownership on every TenantMixin write.

    SECURITY: within an active tenant context, tenant_id is FORCED to the context
    tenant, OVERRIDING any user-supplied value — otherwise a create bound from a
    request body (``Post(**request.json())`` with a forged ``tenant_id``) would
    write into another tenant's space (cross-tenant write / mass assignment). The
    row created or updated inside tenant X's request always belongs to tenant X.

    With NO active context (CLI, migrations, admin, background jobs), the caller
    must set tenant_id explicitly; a never-set field on a strict tenant model is
    refused rather than silently written as a NULL/unowned row.
    """
    if not isinstance(instance, TenantMixin):
        return
    tenant = get_tenant()
    if tenant is not None:
        # Force ownership to the active tenant on both insert and update.
        instance.tenant_id = tenant.tenant_id
        return
    # No active tenant context.
    if created and isinstance(instance.tenant_id, FieldInfo):
        if get_setting("TENANT_STRICT"):
            raise TenantResolutionError(
                "Creating a TenantMixin row with no active tenant context and no "
                "explicit tenant_id — refusing to write an unowned row. Set "
                "tenant_id explicitly (CLI/admin) or run within tenant_context()."
            )


# ── Admin Helper (inject tenant condition into raw SQL) ───────────────────


def inject_tenant_condition(
    model_class: type,
    conditions: list[str],
    params: list,
) -> None:
    """Inject tenant_id filter into raw SQL conditions/params lists.

    No-op if model doesn't use TenantMixin or no tenant context is active.
    Used by admin views that build raw SQL instead of QuerySet.

    Usage in admin:
        inject_tenant_condition(config.model_class, conditions, params)
    """
    if not issubclass(model_class, TenantMixin):
        return
    tenant = get_tenant()
    if tenant is None:
        return
    params.append(tenant.tenant_id)
    conditions.append(f"tenant_id = ${len(params)}")


def tenant_where_suffix(model_class: type, params: list) -> str:
    """Return ' AND tenant_id = $N' suffix for single-record WHERE clauses.

    Returns empty string if model doesn't use TenantMixin or no tenant active.
    Appends tenant_id to params list as a side effect.

    Usage:
        sql = f"SELECT * FROM {table} WHERE {pk} = $1{tenant_where_suffix(model, params)}"
    """
    if not issubclass(model_class, TenantMixin):
        return ""
    tenant = get_tenant()
    if tenant is None:
        return ""
    params.append(tenant.tenant_id)
    return f" AND tenant_id = ${len(params)}"
