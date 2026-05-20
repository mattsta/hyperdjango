"""
Standalone HyperAdmin — auto-generated CRUD admin for HyperApp models.

No Django dependency. Introspects Model field annotations and auto-generates
list/add/edit/delete routes + HTML forms with the Zig template engine.

Usage:
    from hyperdjango import HyperApp, Model, Field
    from hyperdjango.admin import HyperAdmin

    app = HyperApp(title="My App", database="postgres://...", templates="templates")

    class User(Model):
        class Meta:
            table = "users"
        id: int = Field(primary_key=True, auto=True)
        name: str = Field()
        email: str = Field(default="")
        age: int = Field(ge=0, le=150, default=0)
        is_active: bool = Field(default=True)

    admin = HyperAdmin(app, prefix="/admin")
    admin.register(User)
    # Auto-generates:
    #   GET  /admin/              → dashboard (list of registered models)
    #   GET  /admin/user/         → list view (paginated, searchable, sortable)
    #   GET  /admin/user/add/     → create form
    #   POST /admin/user/add/     → create handler
    #   GET  /admin/user/{id}/    → edit form
    #   POST /admin/user/{id}/    → update handler
    #   POST /admin/user/{id}/delete/ → delete handler
"""

# ruff: noqa: F401  — public API re-exports

import contextlib
import hmac
import inspect
import re
import secrets
import time as _time
import typing
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from hyperdjango.conf import ONE_DAY, get_setting
from hyperdjango.logging import logger as _logger
from hyperdjango.native import fast_json_dumps, fast_json_loads

ADMIN_SESSION_COOKIE = "hyper_admin_session"

# Session-hash verify cache lifetime (seconds). Short enough that a password
# change invalidates outstanding sessions within a few seconds, long enough to
# absorb the burst of requests that make up a single page render.
_HASH_VERIFY_TTL = 5.0

# list_filter DISTINCT options cache lifetime (seconds). Filter dropdowns tolerate
# slightly stale option sets, so a short window removes a per-render scan.
_FILTER_DISTINCT_TTL = 30.0

# DoS cap: maximum admin ?q search length. Matches the REST layer's 200-char
# limit — each searchable field is scanned with `::text ILIKE '%q%'`.
_ADMIN_MAX_SEARCH_LENGTH = 200

# FK <select> option cache lifetime (seconds). Same rationale as the filter cache:
# form dropdowns can serve slightly stale option lists to avoid a scan per render.
_FK_DISPLAY_VALUES_TTL = 30.0

_FK_DISPLAY_COLUMNS = ("name", "title", "username", "label", "email")
_LABEL_CANDIDATES = (
    "name",
    "username",
    "title",
    "display_name",
    "codename",
    "label",
    "email",
)
_COVERAGE_COUNT_KEYS = frozenset(("perm_count", "group_count", "user_count"))

# All identifiers interpolated into admin SQL MUST pass this check.
# Values come from model metadata (table names, column names from _LABEL_CANDIDATES),
# never from user input. This regex rejects anything that could break SQL.
_SAFE_IDENT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _assert_safe_ident(value: str) -> str:
    """Validate a SQL identifier (table/column name) is alphanumeric+underscore only."""
    if not _SAFE_IDENT_RE.match(value):
        raise ValueError(f"Unsafe SQL identifier: {value!r}")
    return value


_HOOK_ARITY_CACHE: dict = {}


def _hook_param_count(hook) -> int:
    """Parameter count of a save/delete hook, memoized per function.

    Hooks live for the process lifetime (registered on a ModelConfig), so their
    signature never changes — recomputing inspect.signature() on every request
    was pure overhead. Cached by the (hashable) function object.
    """
    n = _HOOK_ARITY_CACHE.get(hook)
    if n is None:
        n = len(inspect.signature(hook).parameters)
        _HOOK_ARITY_CACHE[hook] = n
    return n


def _optional_int(raw: str) -> int | None:
    """Parse an optional integer query param.

    Empty/absent → None. Present but non-numeric → ValueError (caller maps to a
    400). Keeps raw GET params from reaching ``int()`` unguarded and 500-ing.
    """
    if raw is None or raw == "":
        return None
    return int(raw)


def _parse_pk(pk) -> int | None:
    """Parse an integer path-param PK. Returns None for non-numeric input.

    The admin addresses rows by integer PK everywhere (int(pk) downstream), so a
    non-numeric ``/admin/<slug>/<id>/`` should be a clean 404, not a 500 from an
    uncaught ValueError.
    """
    try:
        return int(pk)
    except ValueError, TypeError:
        return None


from hyperdjango.admin.fields import (
    Action,
    AdminField,
    Fieldset,
    InlineConfig,
    ModelConfig,
    ThemeConfig,
    _introspect_model,
    _type_to_widget,
)
from hyperdjango.admin.templates import (
    _ADMIN_CSS,
    _PARTIAL_PARAMS,
    _PARTIAL_URL,
    _TEMPLATE_FOOTER,
    _TEMPLATE_HEADER,
    TEMPLATE_CACHE_DASHBOARD,
    TEMPLATE_CONFIRM_DELETE,
    TEMPLATE_DASHBOARD,
    TEMPLATE_DELETE_DIALOG,
    TEMPLATE_EFFECTIVE_PERMS,
    TEMPLATE_FIELD_ERROR,
    TEMPLATE_FIELD_VALID,
    TEMPLATE_FORM,
    TEMPLATE_GROUP_TREE,
    TEMPLATE_HISTORY,
    TEMPLATE_INLINE_ROW,
    TEMPLATE_INLINE_SECTION,
    TEMPLATE_LIST,
    TEMPLATE_LIST_PARTIAL,
    TEMPLATE_LOGIN,
    TEMPLATE_PERM_CHECK,
    TEMPLATE_RBAC_AUDIT,
    TEMPLATE_RBAC_DASHBOARD,
    TEMPLATE_RBAC_EXPORT,
)
from hyperdjango.admin.utils import (
    _coerce_value,
    _detect_fk_field,
    _escape_html,
    _get_inline_fields,
)
from hyperdjango.auth.audit import AuditLog
from hyperdjango.auth.passwords import hash_password as _hash_password
from hyperdjango.auth.permissions import (
    PermissionChecker,
    RBACauditLog,
    get_auth_epoch,
    invalidate_user_sessions,
    register_session_invalidation_hook,
)
from hyperdjango.auth.sessions import (
    InMemorySessionStore,
    get_session_auth_hash,
    is_safe_redirect_url,
)
from hyperdjango.auth.user import FieldPermission as AuthFieldPermission
from hyperdjango.auth.user import Group as AuthGroup
from hyperdjango.auth.user import GroupPermission as AuthGroupPermission
from hyperdjango.auth.user import ObjectPermission as AuthObjectPermission
from hyperdjango.auth.user import Permission as AuthPermission
from hyperdjango.auth.user import PermissionRule as AuthPermissionRule
from hyperdjango.auth.user import RBACAuditEntry as AuthRBACAuditEntry
from hyperdjango.auth.user import User as AuthUser
from hyperdjango.auth.user import UserGroup as AuthUserGroup
from hyperdjango.auth.user import UserPermission as AuthUserPermission
from hyperdjango.auth.user import parse_rule_config, rule_config_to_json
from hyperdjango.cache import LocMemCache, get_cache
from hyperdjango.cache_adapters import TwoTierCache
from hyperdjango.database import get_db as _get_db_fallback
from hyperdjango.models import ManyToManyField, _get_db_meta
from hyperdjango.native._crypto import sign_data, verify_signed_data
from hyperdjango.query import _get_model_by_table
from hyperdjango.query_cache import get_query_cache
from hyperdjango.ratelimit import RateLimitRule
from hyperdjango.response import Response
from hyperdjango.telemetry import metrics as _tel_metrics
from hyperdjango.templating import TemplateEngine
from hyperdjango.tenancy import inject_tenant_condition, tenant_where_suffix
from hyperdjango.validation.core.fields import _MISSING, FieldInfo

# ── Native telemetry (zero cost when disabled) ──────────────────────────────
#
# Counts admin write actions by (model_slug, action). Cardinality is bounded
# by the number of registered models × {add, change, delete} ≈ small. Read
# actions are intentionally NOT tracked here — the per-request HTTP metric
# from TelemetryMiddleware already covers /admin/<slug>/ list traffic.

_admin_actions_total = _tel_metrics.CounterVec(
    "hyperdjango_admin_actions_total",
    "HyperAdmin write actions by model and action.",
    label_names=("model", "action"),
)

# ── Field types, templates, and utilities imported from admin submodules ──
# See admin/fields.py, admin/templates.py, admin/utils.py

# ── Display decorator ────────────────────────────────────────────────────


def display(
    *,
    description: str | None = None,
    ordering: str | None = None,
    boolean: bool = False,
    empty_value: str | None = None,
):
    """Decorator for list_display callables to set display properties.

    Usage:
        @display(description="Full Name", ordering="last_name")
        def full_name(obj):
            return f"{obj['first_name']} {obj['last_name']}"

        @display(description="Active?", boolean=True)
        def is_active(obj):
            return obj.get("is_active", False)
    """

    def decorator(func):
        if description is not None:
            func._admin_description = description
        if ordering is not None:
            func._admin_ordering = ordering
        func._admin_boolean = boolean
        if empty_value is not None:
            func._admin_empty_value = empty_value
        return func

    return decorator


# ── HyperAdmin ───────────────────────────────────────────────────────────


class HyperAdmin:
    """Auto-generated CRUD admin for HyperApp models.

    Introspects Model field annotations and generates:
    - Dashboard (list of registered models)
    - List view (paginated, searchable, sortable)
    - Add form (create new instances)
    - Edit form (update existing instances)
    - Delete handler
    """

    def __init__(
        self,
        app,
        prefix="/admin",
        title="HyperAdmin",
        secret_key=None,
        require_auth=True,
    ):
        self.app = app
        self.prefix = prefix.rstrip("/")
        self.title = title
        self._models: dict[str, ModelConfig] = {}
        self._engine = None
        if secret_key is None:
            # Honor the declared ADMIN_SECRET setting (the documented way to pin
            # a persistent admin key) before falling back to an ephemeral one.
            secret_key = get_setting("ADMIN_SECRET")
        if not secret_key:
            secret_key = secrets.token_urlsafe(32)
            _logger.warning(
                "HyperAdmin using auto-generated secret_key (sessions won't survive restart). "
                "Set secret_key= explicitly or the ADMIN_SECRET setting for production."
            )
        self._secret_key = secret_key
        self._require_auth = require_auth
        self._themes: dict[str, ThemeConfig] = {}  # Custom themes registry
        self._perm_checker = None  # Lazy init
        self._session_store = (
            None  # Lazy init (in-memory fallback, db-backed in production)
        )
        self._db_session_store = None  # Database-backed store
        # Session-hash verify cache: session_id -> monotonic expiry. A hit means
        # the session's stored auth hash matched the live password_hash recently,
        # so we can skip the per-request DB round trip until the TTL lapses.
        self._hash_verify_cache: dict[str, float] = {}
        # list_filter DISTINCT options cache: (sql, params) -> (expiry, options)
        self._filter_distinct_cache: dict[tuple, tuple[float, list]] = {}
        # Resolved template cache: (slug, view_type) -> (path_or_None, mtime, source)
        self._template_cache: dict[tuple, tuple] = {}
        # M2M target display-column cache: (slug, field_name) -> display_col
        self._m2m_display_col_cache: dict[tuple, str] = {}
        # FK <select> option cache: fk_table -> (expiry, base_cache, choices)
        self._fk_display_values_cache: dict[str, tuple[float, dict, list]] = {}
        self._has_auth_models = False  # Set by register_auth_models()
        self._has_cache_dashboard = False  # Set by register_cache_dashboard()

        # Register routes
        app.router.add("GET", f"{self.prefix}/", self._dashboard)
        app.router.add("GET", f"{self.prefix}/login/", self._login_page)
        app.router.add("POST", f"{self.prefix}/login/", self._login_handler)
        app.router.add("GET", f"{self.prefix}/logout/", self._logout_handler)

    @property
    def engine(self):
        """Lazy-init template engine for rendering."""
        if self._engine is None:
            self._engine = TemplateEngine(template_dir=".")
        return self._engine

    def register_theme(self, theme: ThemeConfig):
        """Register a custom theme for the admin UI.

        Custom themes add CSS variable overrides accessible via the theme toggle.
        Built-in light/dark themes are always available.

        Args:
            theme: ThemeConfig with name, label, and css_vars dict.

        Usage:
            admin.register_theme(ThemeConfig(
                name="brand",
                label="Brand Purple",
                css_vars={"--primary": "#7c3aed"},
            ))
        """
        self._themes[theme.name] = theme

    def get_theme_css(self, theme_name: str) -> str:
        """Generate CSS for a registered custom theme."""
        theme = self._themes.get(theme_name)
        if theme is None:
            return ""
        props = " ".join(f"{k}: {v};" for k, v in theme.css_vars.items())
        return f'[data-theme="{theme.name}"] {{ {props} }}'

    @property
    def registered_themes(self) -> list[ThemeConfig]:
        """Get all registered custom themes."""
        return list(self._themes.values())

    def register(
        self,
        model_class,
        *,
        list_display=None,
        search_fields=None,
        ordering=None,
        per_page=25,
        readonly_fields=None,
        exclude_fields=None,
        slug=None,
        fieldsets=None,
        list_filter=None,
        list_editable=None,
        actions=None,
        list_display_callables=None,
        save_hooks=None,
        delete_hooks=None,
        list_template=None,
        form_template=None,
        delete_template=None,
        media_css=None,
        media_js=None,
        formfield_overrides=None,
        inlines=None,
        prepopulated_fields=None,
        date_hierarchy=None,
        # Dynamic per-request hooks
        get_queryset=None,
        get_readonly_fields=None,
        get_fieldsets=None,
        get_list_display=None,
        get_search_results=None,
        get_form=None,
        can_view=True,
        # View control
        list_display_links=None,
        view_on_site=None,
        response_add="list",
        response_change="list",
        response_delete="list",
        empty_value_display="-",
        save_as=False,
        save_on_top=False,
        show_full_result_count=True,
        sortable_by=None,
        radio_fields=None,
        raw_id_fields=None,
        autocomplete_fields=None,
        preserve_filters=True,
        filter_horizontal=None,
        on_add=None,
        on_change=None,
        on_delete=None,
    ):
        """Register a Model for admin CRUD.

        Args:
            model_class: HyperApp Model subclass
            list_display: field names (and callable names) to show in list view
            search_fields: field names to search
            ordering: default sort field (prefix with - for descending)
            per_page: items per page in list view
            readonly_fields: fields that can't be edited
            exclude_fields: fields hidden from forms
            slug: URL slug (default: lowercase model name)
            fieldsets: list of Fieldset for grouped form layout
            list_filter: field names for sidebar filter dropdowns
            list_editable: field names editable inline in list view
            actions: list of Action for bulk operations
            save_hooks: list of async callable(values, is_edit) → values, called before save
            delete_hooks: list of async callable(pk) → None, called before delete
            list_template: override template for list view (file path or string)
            form_template: override template for add/edit form (file path or string)
            delete_template: override template for delete confirmation (file path or string)
            media_css: list of CSS file paths to inject in <head>
            media_js: list of JS file paths to inject in <head>
            formfield_overrides: dict mapping Python type → widget config override
            inlines: list of InlineConfig for editing related objects on parent form
            get_queryset: async callable(request) → dict|None for row filtering
            get_readonly_fields: callable(request, obj|None) → list[str]
            get_fieldsets: callable(request, obj|None) → list[Fieldset]
            get_list_display: callable(request) → list[str]
            get_search_results: async callable(request, conditions, term) → dict|None
            get_form: callable(request, obj|None) → dict with fields/required overrides
            can_view: allow view-only access (default True)
            list_display_links: which columns link to edit (None = no links, default = first)
            view_on_site: callable(obj_dict) → str URL for 'View on site' link
            response_add/change/delete: "list"|"continue"|"add"|callable(request,obj)→url
            empty_value_display: string shown for NULL/empty values (default "-")
            save_as: show "Save as new" button on edit form
            save_on_top: show save buttons at top of form
            show_full_result_count: show total count on filtered list views
            sortable_by: restrict which columns are sortable (None = all)
            radio_fields: dict {"field": "horizontal"|"vertical"} for radio button rendering
            raw_id_fields: FK fields shown as plain number input
            autocomplete_fields: FK fields with HTMX autocomplete (None = all FKs)
            preserve_filters: keep filter state across edit navigation
            on_add/on_change/on_delete: async callbacks after DB write + audit log
        """
        model_slug = slug or model_class.__name__.lower()
        fields = _introspect_model(model_class)

        # Always include built-in delete action
        all_actions = list(actions or [])
        has_delete = any(a.name == "delete_selected" for a in all_actions)
        if not has_delete:
            all_actions.insert(
                0,
                Action(
                    name="delete_selected",
                    label="Delete selected",
                    handler=self._builtin_delete_action,
                    confirm=True,
                ),
            )

        # System checks — validate config at registration time
        field_names = {f.name for f in fields}
        self._check_config(
            model_class.__name__,
            field_names,
            list_display=list_display,
            search_fields=search_fields,
            list_filter=list_filter,
            list_editable=list_editable,
            readonly_fields=readonly_fields,
            fieldsets=fieldsets,
        )

        config = ModelConfig(
            model_class=model_class,
            slug=model_slug,
            name=model_class.__name__,
            fields=fields,
            list_display=list_display,
            search_fields=search_fields,
            ordering=ordering,
            per_page=per_page,
            readonly_fields=readonly_fields or [],
            exclude_fields=exclude_fields or [],
            fieldsets=fieldsets,
            list_filter=list_filter or [],
            list_editable=list_editable or [],
            actions=all_actions,
            list_display_callables=list_display_callables or {},
            save_hooks=save_hooks or [],
            delete_hooks=delete_hooks or [],
            list_template=list_template,
            form_template=form_template,
            delete_template=delete_template,
            media_css=media_css or [],
            media_js=media_js or [],
            formfield_overrides=formfield_overrides or {},
            inlines=inlines or [],
            prepopulated_fields=prepopulated_fields or {},
            date_hierarchy=date_hierarchy,
            get_queryset=get_queryset,
            get_readonly_fields=get_readonly_fields,
            get_fieldsets=get_fieldsets,
            get_list_display=get_list_display,
            get_search_results=get_search_results,
            get_form=get_form,
            can_view=can_view,
            list_display_links=list_display_links,
            view_on_site=view_on_site,
            response_add=response_add,
            response_change=response_change,
            response_delete=response_delete,
            empty_value_display=empty_value_display,
            save_as=save_as,
            save_on_top=save_on_top,
            show_full_result_count=show_full_result_count,
            sortable_by=sortable_by,
            radio_fields=radio_fields or {},
            raw_id_fields=raw_id_fields or [],
            autocomplete_fields=autocomplete_fields,
            preserve_filters=preserve_filters,
            filter_horizontal=filter_horizontal or [],
            on_add=on_add,
            on_change=on_change,
            on_delete=on_delete,
        )

        # Auto-detect FK fields for inlines
        for inline in config.inlines:
            if inline.fk_field is None:
                inline.fk_field = _detect_fk_field(inline.model_class, model_class)

        self._models[model_slug] = config

        # Register CRUD routes
        p = self.prefix
        aw = self._auth_wrap
        self.app.router.add(
            "GET", f"{p}/{model_slug}/", aw(self._make_list_view(config))
        )
        self.app.router.add(
            "POST", f"{p}/{model_slug}/", aw(self._make_list_action_handler(config))
        )
        self.app.router.add(
            "GET", f"{p}/{model_slug}/add/", aw(self._make_add_view(config))
        )
        self.app.router.add(
            "POST", f"{p}/{model_slug}/add/", aw(self._make_add_handler(config))
        )
        self.app.router.add(
            "GET", f"{p}/{model_slug}/{{id}}/", aw(self._make_edit_view(config))
        )
        self.app.router.add(
            "POST", f"{p}/{model_slug}/{{id}}/", aw(self._make_edit_handler(config))
        )
        self.app.router.add(
            "POST",
            f"{p}/{model_slug}/{{id}}/delete/",
            aw(self._make_delete_handler(config)),
        )
        # list_editable save endpoint
        if config.list_editable:
            self.app.router.add(
                "POST",
                f"{p}/{model_slug}/save-list/",
                aw(self._make_save_list_handler(config)),
            )
        # HTMX endpoints
        self.app.router.add(
            "GET", f"{p}/{model_slug}/partial/", aw(self._make_partial_view(config))
        )
        self.app.router.add(
            "POST",
            f"{p}/{model_slug}/validate/",
            aw(self._make_validate_handler(config)),
        )
        self.app.router.add(
            "GET",
            f"{p}/{model_slug}/autocomplete/",
            aw(self._make_autocomplete_handler(config)),
        )
        self.app.router.add(
            "GET",
            f"{p}/{model_slug}/{{id}}/confirm-delete/",
            aw(self._make_confirm_delete_dialog(config)),
        )
        # History view
        self.app.router.add(
            "GET",
            f"{p}/{model_slug}/{{id}}/history/",
            aw(self._make_history_view(config)),
        )
        # Inline row endpoint (HTMX — returns a single form row)
        if config.inlines:
            self.app.router.add(
                "GET",
                f"{p}/{model_slug}/inline-row/",
                aw(self._make_inline_row_handler(config)),
            )

        return config

    def model_action(
        self,
        slug: str,
        action_name: str,
        method: str = "POST",
        permission: str | None = None,
    ):
        """Decorator to register a custom URL endpoint under a model's admin section.

        Usage:
            @admin.model_action("order", "ship", method="POST")
            async def ship_order(request, pk):
                ...

            @admin.model_action("report", "export", method="GET")
            async def export_report(request):
                ...

        Registers URL: {prefix}/{slug}/{pk}/{action_name}/ (if handler accepts pk)
                    or: {prefix}/{slug}/{action_name}/ (if handler has no pk param)

        SECURITY: mutating actions (method POST/PUT/PATCH/DELETE) are routed
        through the same ``_enforce_post_security`` the built-in add/edit/delete
        handlers use — CSRF HMAC verification plus a per-model permission check —
        so a developer-registered mutating endpoint can no longer bypass CSRF or
        the model's permission gate with only an is_staff check. ``permission``
        selects the per-model flag enforced (``"can_add"``, ``"can_change"``
        (default), ``"can_delete"``, ``"can_view"``); it applies when ``slug`` is
        a registered admin model. For a mutating action on a NON-model slug
        (e.g. a "report" endpoint) there is no per-model perm to check, so CSRF
        alone is enforced on top of the staff gate. GET (and other non-mutating)
        actions stay is_staff/view-gated exactly as before — the handler calling
        convention is unchanged in every case (``handler(request)``; path params
        via ``request.path_params``).
        """
        method_u = method.upper()
        is_mutating = method_u in ("POST", "PUT", "PATCH", "DELETE")
        required_perm = permission or "can_change"

        def decorator(func):
            params = inspect.signature(func).parameters
            has_pk = "pk" in params or "id" in params
            if has_pk:
                path = f"{self.prefix}/{slug}/{{id}}/{action_name}/"
            else:
                path = f"{self.prefix}/{slug}/{action_name}/"
            if is_mutating:
                wrapped = self._auth_wrap_action(func, slug, required_perm)
            else:
                wrapped = self._auth_wrap(func)
            self.app.router.add(method_u, path, wrapped)
            return func

        return decorator

    async def ensure_search_indexes(self):
        """Auto-create indexes for search_fields and autocomplete columns.

        Admin search and autocomplete both use CONTAINS matching
        (``col::text ILIKE '%q%'``), so a prefix B-tree index
        (varchar_pattern_ops) can never be used — only a leading-anchored
        pattern qualifies for those. To accelerate substring ILIKE we build
        pg_trgm GIN indexes (``USING gin (col gin_trgm_ops)``), which Postgres
        can use for ``%q%`` matches.

        The pg_trgm extension is required. We attempt to enable it; if it is
        absent and cannot be created (e.g. insufficient privileges on a managed
        instance), we skip index creation entirely rather than build indexes the
        planner would ignore.

        Safe to call multiple times — uses CREATE INDEX IF NOT EXISTS.
        Should be called during app startup after all models are registered.
        """
        db = self._get_db()
        if db is None:
            return []

        # Ensure pg_trgm is available; without it, gin_trgm_ops does not exist
        # and every CREATE INDEX below would fail. Skip cleanly in that case.
        # May lack privilege; fall through to the availability probe either way.
        with contextlib.suppress(Exception):
            await db.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        try:
            has_trgm = await db.query_val(
                "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm')"
            )
        # blind-except: pg_trgm capability probe; any DB error (missing extension/no privilege) means the feature is absent and search degrades to a sequential scan rather than failing index setup.
        except Exception:
            has_trgm = False
        if not has_trgm:
            _logger.info(
                "admin: pg_trgm unavailable; skipping search index creation "
                "(substring ILIKE search will fall back to sequential scan)"
            )
            return []

        def _trgm_index_sql(idx_name: str, table: str, col: str) -> str:
            return (
                f"CREATE INDEX IF NOT EXISTS {idx_name} "
                f"ON {table} USING gin (({col}::text) gin_trgm_ops)"
            )

        created = []
        for model_slug, config in self._models.items():
            table = config.model_class._meta.table
            search_fields = config.searchable_fields or []

            for field_name in search_fields:
                # Sanitize: only allow alphanumeric + underscore
                if not field_name.replace("_", "").isalnum():
                    continue
                idx_name = f"idx_{table}_{field_name}_trgm"
                try:
                    await db.execute(_trgm_index_sql(idx_name, table, field_name))
                    created.append(idx_name)
                # blind-except: opportunistic index creation; a missing table or incompatible column just skips that index — search still works without it, so it must not abort admin startup.
                except Exception:
                    pass  # Table may not exist yet, or column type incompatible

            # Also create index for autocomplete display columns on FK targets
            for af in config.fields:
                if not af.foreign_key:
                    continue
                related_table = af.foreign_key
                # Find display column for the related table
                related_model = _get_model_by_table(related_table)
                if related_model is None:
                    continue
                display_col = None
                for col_name in _FK_DISPLAY_COLUMNS:
                    if col_name in related_model._meta.fields:
                        display_col = col_name
                        break
                if display_col is None:
                    continue
                idx_name = f"idx_{related_table}_{display_col}_trgm_ac"
                try:
                    await db.execute(
                        _trgm_index_sql(idx_name, related_table, display_col)
                    )
                    created.append(idx_name)
                # blind-except: opportunistic autocomplete index; a missing FK table or incompatible column just skips it without breaking admin startup.
                except Exception:
                    pass

        return created

    def register_auth_models(self):
        """Register User, Group, Permission for self-managing admin.

        The admin can manage its own users, groups, and permissions.
        Users get a special password change handler (never show raw hash).
        """
        User, Group, Permission = AuthUser, AuthGroup, AuthPermission
        UserGroup, GroupPermission = AuthUserGroup, AuthGroupPermission
        UserPermission = AuthUserPermission

        # ── User management ──────────────────────────────────────────────
        async def escalation_guard(request, values, is_edit, obj):
            """Prevent non-superuser staff from escalating is_superuser/is_staff.

            On edit: if the current admin user is not superuser, strip any
            changes to is_superuser and is_staff (preserve original values).
            On add: non-superusers cannot create superusers or staff.

            The acting user is read from ``request._admin_user`` (set per-request
            during auth) rather than any instance-level slot. Under free-threaded
            / concurrent execution a shared instance slot could be overwritten by
            another request between auth and save, so the guard MUST rely solely
            on the request that is being processed. Fail closed: an unknown or
            missing user is treated as a non-superuser.
            """
            # _admin_user is session data (a dict), set per-request during auth —
            # dict access, consistent with _get_model_perms/_load_user_permissions.
            current_user = request._admin_user
            is_current_superuser = bool(
                current_user and current_user.get("is_superuser")
            )

            if not is_current_superuser:
                if is_edit:
                    # Restore the original privilege flags from the live row
                    # (``obj``). The auto ``id`` is stripped from ``values`` by
                    # _parse_form_data, so ``values.get("id")`` is always None —
                    # ``obj`` is the only carrier of the real id + original flags.
                    # Fail closed: if the live row can't be resolved, force both
                    # flags off rather than trusting attacker-submitted values.
                    orig = obj if isinstance(obj, dict) else None
                    if orig is None or orig.get("id") is None:
                        values["is_superuser"] = False
                        values["is_staff"] = False
                    else:
                        values["is_superuser"] = orig.get("is_superuser", False)
                        values["is_staff"] = orig.get("is_staff", False)
                else:
                    # Non-superuser cannot create superusers OR staff
                    values["is_superuser"] = False
                    values["is_staff"] = False
            return values

        async def hash_password_hook(values, is_edit):
            """Hash password on create, skip on edit unless explicitly set.

            On password change (edit with new password), invalidates all old
            sessions for this user via session auth hash mismatch. The next
            request with an old session will fail the hash check and redirect
            to login.
            """
            raw = values.pop("_new_password", None)
            if raw:
                values["password_hash"] = _hash_password(raw)
                # Invalidate old sessions: compute new session auth hash,
                # then delete all sessions with a different hash.
                if is_edit:
                    user_id = values.get("id")
                    if user_id:
                        new_hash = get_session_auth_hash(
                            values["password_hash"], self._secret_key
                        )
                        store = self._get_session_store()
                        store.invalidate_by_hash(user_id, new_hash)
            elif not is_edit:
                # New user without password → unusable
                if "password_hash" not in values or not values["password_hash"]:
                    values["password_hash"] = ""
            return values

        async def invalidate_on_privilege_change(request, values, is_edit, obj):
            """Invalidate a user's live sessions when their privilege/active
            flags change on edit.

            De-escalation (is_superuser/is_staff → False) or deactivation
            (is_active → False) must take effect immediately, not at session
            expiry. Runs after escalation_guard so it compares the FINAL values
            against the live row (``obj``). Absent-in-``values`` fields default to
            the old value to avoid needless logouts on unrelated edits (e.g. an
            email change). Returns ``values`` unchanged — this hook only observes.
            """
            if is_edit and isinstance(obj, dict):
                uid = obj.get("id")
                if uid is not None:
                    changed = any(
                        str(obj.get(flag)) != str(values.get(flag, obj.get(flag)))
                        for flag in ("is_staff", "is_superuser", "is_active")
                    )
                    if changed:
                        await invalidate_user_sessions(uid)
            return values

        # ── Bulk actions ────────────────────────────────────────────────
        async def _bulk_add_to_group(group_name, selected_ids):
            """Add users to a group without the per-user SELECT+INSERT N+1.

            One query resolves the group, one query fetches the memberships that
            already exist, and one multi-VALUES INSERT (in a transaction) adds
            the rest.
            """
            db = self._get_db()
            grp = await Group.objects.using(db).filter(name=group_name).first()
            if grp is None:
                return f"No '{group_name}' group found. Run 'hyper setup' first."

            uids = [int(u) for u in selected_ids if u]
            if not uids:
                return f"Added 0 user(s) to {group_name} group."

            placeholders = ", ".join(f"${i + 2}" for i in range(len(uids)))
            existing_rows = await db.query(
                f"SELECT user_id FROM hyper_user_groups "
                f"WHERE group_id = $1 AND user_id IN ({placeholders})",
                grp.id,
                *uids,
            )
            existing = {
                (r["user_id"] if isinstance(r, dict) else r[0]) for r in existing_rows
            }
            missing = [u for u in uids if u not in existing]
            if missing:
                values_sql = ", ".join(f"($1, ${i + 2})" for i in range(len(missing)))
                async with db.transaction():
                    await db.execute(
                        f"INSERT INTO hyper_user_groups (group_id, user_id) "
                        f"VALUES {values_sql}",
                        grp.id,
                        *missing,
                    )
            # This raw INSERT bypasses PermissionChecker.add_user_to_group, so
            # invalidate the affected users' live sessions here: their group
            # membership (and thus is_staff/is_superuser snapshot) changed and
            # must be re-derived on their next request.
            for uid in missing:
                await invalidate_user_sessions(uid)
            return f"Added {len(missing)} user(s) to {group_name} group."

        def _requester_is_superuser(request):
            # _admin_user is per-request session data (a dict). Fail closed:
            # a missing/unknown user is treated as non-superuser.
            current_user = request._admin_user
            return bool(current_user and current_user.get("is_superuser"))

        async def add_to_staff_group(config, request, selected_ids):
            # Privilege-granting bulk action: the list action handler only
            # checks can_delete, so gate escalation behind an explicit
            # superuser check (mirrors escalation_guard on the edit path).
            if not _requester_is_superuser(request):
                return "Only superusers can grant staff access."
            return await _bulk_add_to_group("staff", selected_ids)

        async def add_to_superuser_group(config, request, selected_ids):
            if not _requester_is_superuser(request):
                return "Only superusers can grant superuser access."
            return await _bulk_add_to_group("superuser", selected_ids)

        async def deactivate_users(config, request, selected_ids):
            db = self._get_db()
            ids = [int(i) for i in selected_ids]
            count = (
                await User.objects.using(db).filter(id__in=ids).update(is_active=False)
            )
            # SECURITY: a deactivated user must not keep a live session. Their
            # session snapshot has is_active frozen True; invalidate now so the
            # next request forces re-auth (which then fails: is_active=False).
            for uid in ids:
                await invalidate_user_sessions(uid)
            return f"Deactivated {count} user(s)."

        self.register(
            User,
            slug="users",
            list_display=["username", "email", "is_staff", "is_superuser", "is_active"],
            search_fields=["username", "email", "first_name", "last_name"],
            list_filter=["is_staff", "is_superuser", "is_active"],
            fieldsets=[
                Fieldset(
                    title="Account", fields=["username", "email", "_new_password"]
                ),
                Fieldset(title="Personal", fields=["first_name", "last_name"]),
                Fieldset(
                    title="Permissions",
                    fields=["is_active", "is_staff", "is_superuser"],
                ),
            ],
            exclude_fields=["password_hash", "last_login", "created_at"],
            readonly_fields=["last_login", "created_at"],
            save_hooks=[
                hash_password_hook,
                escalation_guard,
                invalidate_on_privilege_change,
            ],
            ordering="-id",
            inlines=[
                InlineConfig(model_class=UserGroup, fields=["group_id"]),
                InlineConfig(model_class=UserPermission, fields=["permission_id"]),
            ],
            actions=[
                Action("add_to_staff", "Add to staff group", add_to_staff_group),
                Action(
                    "add_to_superuser", "Add to superuser group", add_to_superuser_group
                ),
                Action(
                    "deactivate", "Deactivate selected", deactivate_users, confirm=True
                ),
            ],
        )

        # Inject the _new_password virtual field into the User config
        user_config = self._models["users"]
        pw_field = AdminField(
            name="_new_password",
            label="Password",
            python_type=str,
            widget="password",
            required=False,
            default=None,
            attrs={
                "placeholder": "Leave blank to keep current",
                "autocomplete": "new-password",
            },
            choices=None,
            is_pk=False,
            is_auto=False,
            is_readonly=False,
            foreign_key=None,
        )
        user_config.fields.append(pw_field)

        # ── Group/Role management (hierarchical RBAC) ────────────────────
        self.register(
            Group,
            slug="groups",
            list_display=["name", "parent_id", "priority"],
            search_fields=["name"],
            fieldsets=[
                Fieldset(title="Group", fields=["name"]),
                Fieldset(title="Hierarchy", fields=["parent_id", "priority"]),
            ],
            inlines=[
                InlineConfig(model_class=GroupPermission, fields=["permission_id"])
            ],
        )

        # ── Permission management ────────────────────────────────────────
        self.register(
            Permission,
            slug="permissions",
            list_display=["codename", "name", "model_name"],
            search_fields=["codename", "name", "model_name"],
            list_filter=["model_name"],
            ordering="model_name",
        )

        # ── Object-level permissions ────────────────────────────────────
        self.register(
            AuthObjectPermission,
            slug="object-permissions",
            list_display=[
                "permission_id",
                "object_model",
                "object_id",
                "user_id",
                "group_id",
            ],
            search_fields=["object_model", "object_id"],
            list_filter=["object_model"],
            fieldsets=[
                Fieldset(title="Target", fields=["object_model", "object_id"]),
                Fieldset(title="Permission", fields=["permission_id"]),
                Fieldset(title="Grant To", fields=["user_id", "group_id"]),
            ],
        )

        # ── Conditional rules ───────────────────────────────────────────
        # Save hook: assemble structured _rc_* fields into rule_config JSON
        async def rule_config_hook(values, is_edit):
            rule_type = values.get("rule_type", "")
            config = {}
            if rule_type == "is_owner":
                config["owner_field"] = (
                    values.pop("_rc_owner_field", "user_id") or "user_id"
                )
            elif rule_type == "time_window":
                config["start"] = values.pop("_rc_start", "09:00") or "09:00"
                config["end"] = values.pop("_rc_end", "17:00") or "17:00"
                config["timezone"] = values.pop("_rc_timezone", "UTC") or "UTC"
            elif rule_type == "ip_range":
                raw = values.pop("_rc_ranges", "") or ""
                config["ranges"] = [r.strip() for r in raw.split("\n") if r.strip()]
            elif rule_type == "field_match":
                config["field"] = values.pop("_rc_field", "") or ""
                raw = values.pop("_rc_values", "") or ""
                config["values"] = [v.strip() for v in raw.split("\n") if v.strip()]
            elif rule_type == "custom":
                config["module"] = values.pop("_rc_module", "") or ""
                config["function"] = values.pop("_rc_function", "") or ""
            # Clean up any remaining _rc_ fields
            for k in list(values):
                if k.startswith("_rc_"):
                    values.pop(k)
            # Only overwrite rule_config if structured fields were provided
            if config:
                values["rule_config"] = rule_config_to_json(
                    parse_rule_config(rule_type, config)
                )
            return values

        self.register(
            AuthPermissionRule,
            slug="permission-rules",
            list_display=[
                "rule_type",
                "permission_id",
                "is_deny",
                "priority",
                "group_id",
                "user_id",
            ],
            search_fields=["rule_type"],
            list_filter=["rule_type", "is_deny"],
            fieldsets=[
                Fieldset(title="Rule", fields=["permission_id", "rule_type"]),
                Fieldset(
                    title="Rule Configuration",
                    fields=[
                        "_rc_owner_field",
                        "_rc_start",
                        "_rc_end",
                        "_rc_timezone",
                        "_rc_ranges",
                        "_rc_field",
                        "_rc_values",
                        "_rc_module",
                        "_rc_function",
                    ],
                ),
                Fieldset(title="Scope", fields=["group_id", "user_id"]),
                Fieldset(title="Behavior", fields=["priority", "is_deny"]),
            ],
            exclude_fields=["rule_config"],
            save_hooks=[rule_config_hook],
        )

        # Inject virtual fields for rule configuration
        rule_cfg = self._models["permission-rules"]
        virtual_fields = [
            AdminField(
                name="_rc_owner_field",
                label="Owner Field",
                python_type=str,
                widget="text",
                required=False,
                default="user_id",
                attrs={"placeholder": "e.g. user_id, author_id"},
                choices=None,
                is_pk=False,
                is_auto=False,
                is_readonly=False,
                foreign_key=None,
            ),
            AdminField(
                name="_rc_start",
                label="Start Time",
                python_type=str,
                widget="text",
                required=False,
                default="09:00",
                attrs={"placeholder": "HH:MM"},
                choices=None,
                is_pk=False,
                is_auto=False,
                is_readonly=False,
                foreign_key=None,
            ),
            AdminField(
                name="_rc_end",
                label="End Time",
                python_type=str,
                widget="text",
                required=False,
                default="17:00",
                attrs={"placeholder": "HH:MM"},
                choices=None,
                is_pk=False,
                is_auto=False,
                is_readonly=False,
                foreign_key=None,
            ),
            AdminField(
                name="_rc_timezone",
                label="Timezone",
                python_type=str,
                widget="text",
                required=False,
                default="UTC",
                attrs={"placeholder": "e.g. UTC, America/New_York"},
                choices=None,
                is_pk=False,
                is_auto=False,
                is_readonly=False,
                foreign_key=None,
            ),
            AdminField(
                name="_rc_ranges",
                label="IP Ranges (one CIDR per line)",
                python_type=str,
                widget="textarea",
                required=False,
                default="",
                attrs={"rows": "3", "placeholder": "10.0.0.0/8\n192.168.0.0/16"},
                choices=None,
                is_pk=False,
                is_auto=False,
                is_readonly=False,
                foreign_key=None,
            ),
            AdminField(
                name="_rc_field",
                label="Match Field",
                python_type=str,
                widget="text",
                required=False,
                default="",
                attrs={"placeholder": "e.g. status"},
                choices=None,
                is_pk=False,
                is_auto=False,
                is_readonly=False,
                foreign_key=None,
            ),
            AdminField(
                name="_rc_values",
                label="Allowed Values (one per line)",
                python_type=str,
                widget="textarea",
                required=False,
                default="",
                attrs={"rows": "3", "placeholder": "draft\nreview"},
                choices=None,
                is_pk=False,
                is_auto=False,
                is_readonly=False,
                foreign_key=None,
            ),
            AdminField(
                name="_rc_module",
                label="Module Path",
                python_type=str,
                widget="text",
                required=False,
                default="",
                attrs={"placeholder": "e.g. myapp.rules"},
                choices=None,
                is_pk=False,
                is_auto=False,
                is_readonly=False,
                foreign_key=None,
            ),
            AdminField(
                name="_rc_function",
                label="Function Name",
                python_type=str,
                widget="text",
                required=False,
                default="",
                attrs={"placeholder": "e.g. check_budget_limit"},
                choices=None,
                is_pk=False,
                is_auto=False,
                is_readonly=False,
                foreign_key=None,
            ),
        ]
        for vf in virtual_fields:
            rule_cfg.fields.append(vf)

        # ── Field-level permissions ─────────────────────────────────────
        self.register(
            AuthFieldPermission,
            slug="field-permissions",
            list_display=["model_name", "field_name", "access", "group_id", "user_id"],
            search_fields=["model_name", "field_name"],
            list_filter=["model_name", "access"],
            fieldsets=[
                Fieldset(title="Field", fields=["model_name", "field_name"]),
                Fieldset(title="Access", fields=["access"]),
                Fieldset(title="Grant To", fields=["group_id", "user_id"]),
            ],
        )

        # ── Post-registration field overrides (choices for select widgets) ──
        # PermissionRule: rule_type → dropdown, rule_config → textarea
        rule_cfg = self._models["permission-rules"]
        for f in rule_cfg.fields:
            if f.name == "rule_type":
                f.choices = [
                    ("is_owner", "Is Owner"),
                    ("time_window", "Time Window"),
                    ("ip_range", "IP Range"),
                    ("field_match", "Field Match"),
                    ("custom", "Custom"),
                ]
                f.widget = "select"

        # FieldPermission: access → dropdown
        fp_cfg = self._models["field-permissions"]
        for f in fp_cfg.fields:
            if f.name == "access":
                f.choices = [
                    ("hidden", "Hidden"),
                    ("readonly", "Read Only"),
                    ("writable", "Writable"),
                ]
                f.widget = "select"

        # ── RBAC management views (routes) ────────────────────────────────
        p = self.prefix
        aw = self._auth_wrap

        # Effective permissions: GET /admin/users/{id}/effective-permissions/
        self.app.router.add(
            "GET",
            f"{p}/users/{{id}}/effective-permissions/",
            aw(self._make_effective_perms_view()),
        )

        # Permission decision checker: GET/POST /admin/permission-check/
        self.app.router.add(
            "GET", f"{p}/permission-check/", aw(self._make_perm_check_view())
        )
        self.app.router.add(
            "POST", f"{p}/permission-check/", aw(self._make_perm_check_handler())
        )

        # Group hierarchy tree: GET /admin/groups/tree/
        self.app.router.add(
            "GET", f"{p}/groups/tree/", aw(self._make_group_tree_view())
        )

        # RBAC audit log: GET /admin/rbac-audit/
        self.app.router.add("GET", f"{p}/rbac-audit/", aw(self._make_rbac_audit_view()))

        # RBAC policy export/import: GET /admin/rbac-policy/
        self.app.router.add(
            "GET", f"{p}/rbac-policy/", aw(self._make_rbac_policy_view())
        )
        self.app.router.add(
            "GET", f"{p}/rbac-export/download/", aw(self._make_rbac_export_handler())
        )
        self.app.router.add(
            "POST", f"{p}/rbac-import/", aw(self._make_rbac_import_handler())
        )

        # RBAC dashboard: GET /admin/rbac-dashboard/
        self.app.router.add(
            "GET", f"{p}/rbac-dashboard/", aw(self._make_rbac_dashboard_view())
        )

        # RBAC audit log: browse all permission changes
        self.register(
            AuthRBACAuditEntry,
            slug="rbac-audit-log",
            list_display=[
                "id",
                "action",
                "target_type",
                "target_id",
                "actor_username",
                "created_at",
            ],
            search_fields=["action", "actor_username", "target_type", "target_id"],
            list_filter=["action", "target_type"],
            readonly_fields=[
                "action",
                "target_type",
                "target_id",
                "actor_user_id",
                "actor_username",
                "detail",
                "created_at",
                "updated_at",
            ],
            ordering="-id",
        )

        self._has_auth_models = True
        return self

    def register_ratelimit_models(self):
        """Register RateLimitRule for admin CRUD management.

        Provides a UI for managing per-path, per-method, per-tier rate limit rules.
        """
        self.register(
            RateLimitRule,
            slug="rate-limit-rules",
            list_display=[
                "name",
                "path_pattern",
                "method",
                "tier",
                "max_requests",
                "window_seconds",
                "cost",
                "priority",
                "is_active",
            ],
            search_fields=["name", "path_pattern"],
            list_filter=["method", "tier", "is_active"],
            ordering="-priority",
            fieldsets=[
                Fieldset(title="Rule", fields=["name", "is_active"]),
                Fieldset(title="Matching", fields=["path_pattern", "method", "tier"]),
                Fieldset(
                    title="Limits", fields=["max_requests", "window_seconds", "cost"]
                ),
                Fieldset(title="Priority", fields=["priority"]),
            ],
        )

        # Set up dropdown choices for method and tier
        rule_cfg = self._models.get("rate-limit-rules") or self._models.get(
            "ratelimitrule"
        )
        if rule_cfg:
            for f in rule_cfg.fields:
                if f.name == "method":
                    f.choices = [
                        ("*", "All Methods"),
                        ("GET", "GET"),
                        ("POST", "POST"),
                        ("PUT", "PUT"),
                        ("PATCH", "PATCH"),
                        ("DELETE", "DELETE"),
                    ]
                    f.widget = "select"
                elif f.name == "is_active":
                    f.choices = [("true", "Active"), ("false", "Inactive")]
                    f.widget = "select"

        return self

    def register_cache_dashboard(self):
        """Register cache monitoring dashboard at /admin/cache/.

        Displays real-time stats from QueryCacheManager, LocMemCache,
        and TwoTierCache (if configured). Auto-refreshes every 5 seconds.
        """
        p = self.prefix
        aw = self._auth_wrap

        self.app.router.add("GET", f"{p}/cache/", aw(self._make_cache_view()))
        self.app.router.add("GET", f"{p}/cache/json", aw(self._make_cache_json()))

        self._has_cache_dashboard = True
        return self

    def _collect_cache_stats(self) -> dict[str, dict[str, int | float | list | str]]:
        """Gather stats from all cache subsystems."""
        stats: dict[str, dict[str, int | float | list | str]] = {}

        # Query cache
        qc = get_query_cache()
        qc_stats = qc.stats
        table_versions = qc.get_table_versions()
        total = qc_stats.hits + qc_stats.misses
        hit_rate = qc_stats.hits / total if total else 0.0
        stats["query_cache"] = {
            "hits": qc_stats.hits,
            "misses": qc_stats.misses,
            "total_requests": total,
            "hit_rate": f"{hit_rate:.1%}",
            "hit_rate_float": hit_rate,
            "invalidations": qc_stats.invalidations,
            "table_invalidations": qc_stats.table_invalidations,
            "row_invalidations": qc_stats.row_invalidations,
            "sets": qc_stats.sets,
            "table_count": len(table_versions),
        }
        stats["table_versions"] = [
            {"name": name, "version": ver}
            for name, ver in sorted(table_versions.items())
        ]

        # General cache
        cache = get_cache()
        if isinstance(cache, LocMemCache):
            entry_count = cache.count()
            stats["locmem"] = {
                "entry_count": entry_count,
                "max_size": cache.max_size,
                "utilization": f"{entry_count / cache.max_size:.1%}"
                if cache.max_size
                else "0%",
            }

        # TwoTierCache
        if isinstance(cache, TwoTierCache):
            two_tier = cache.get_stats()
            two_tier["l1_hit_rate_pct"] = int(two_tier["l1_hit_rate"] * 100)
            two_tier["l2_hit_rate_pct"] = int(two_tier["l2_hit_rate"] * 100)
            two_tier["overall_hit_rate_pct"] = int(two_tier["overall_hit_rate"] * 100)
            stats["two_tier"] = two_tier

        return stats

    def _make_cache_view(self):
        """Build the cache dashboard HTML view."""
        admin = self

        async def view(request):
            stats = admin._collect_cache_stats()
            ctx = admin._base_context()
            ctx["title"] = "Cache Dashboard"
            ctx["stats"] = stats
            html = admin._render(TEMPLATE_CACHE_DASHBOARD, ctx)
            return Response.html(html)

        return view

    def _make_cache_json(self):
        """Build the cache dashboard JSON API endpoint."""
        admin = self

        async def view(request):
            stats = admin._collect_cache_stats()
            return Response.json(stats)

        return view

    # ── RBAC UI views ──────────────────────────────────────────────────

    def _make_effective_perms_view(self):
        """Build the effective permissions view for a user."""
        admin = self

        async def view(request, id: int):
            db = admin._get_db()
            checker = PermissionChecker(db)
            data = await checker.explain_effective_permissions(id)

            if data.get("user") is None:
                return Response.html("<h1>User not found</h1>", status=404)

            user = data["user"]
            ctx = admin._base_context()
            ctx["title"] = f"Effective Permissions — {user.get('username', 'Unknown')}"
            ctx["user_info"] = user
            ctx["groups"] = data["groups"]
            ctx["direct_perms"] = data["direct_permissions"]
            ctx["inherited_perms"] = data["inherited_permissions"]
            ctx["object_perms"] = data["object_permissions"]
            ctx["rules"] = data["rules"]
            ctx["field_access"] = data["field_access"]
            ctx["back_url"] = f"{admin.prefix}/users/{id}/"

            html = admin._render(TEMPLATE_EFFECTIVE_PERMS, ctx)
            return Response.html(html)

        return view

    def _make_perm_check_view(self):
        """Build the permission checker form view."""
        admin = self

        async def view(request):
            ctx = admin._base_context()
            ctx["title"] = "Permission Checker"
            ctx["result"] = None
            ctx["models"] = list(admin._models.keys())
            html = admin._render(TEMPLATE_PERM_CHECK, ctx)
            return Response.html(html)

        return view

    def _make_perm_check_handler(self):
        """Handle permission check form submission."""
        admin = self

        async def handler(request):
            await request.form()
            form = request._form_data

            user_id = int(form.get("user_id", "0"))
            perm = form.get("perm", "")
            model_name = form.get("model_name", "")

            db = admin._get_db()
            checker = PermissionChecker(db)
            user_dict = await checker.get_user_by_id(user_id)

            result = None
            if user_dict:

                class UserProxy:
                    pass

                u = UserProxy()
                for k, v in user_dict.items():
                    # dynamic-attr: project a runtime DB row dict onto a UserProxy so the checker can read .id/.is_active etc. off it
                    setattr(u, k, v)
                result = await checker.explain_permission_decision(u, perm, model_name)
                result["username"] = user_dict.get("username", "")

            ctx = admin._base_context()
            ctx["title"] = "Permission Checker"
            ctx["result"] = result
            ctx["models"] = list(admin._models.keys())
            ctx["form_user_id"] = user_id
            ctx["form_perm"] = perm
            ctx["form_model"] = model_name
            html = admin._render(TEMPLATE_PERM_CHECK, ctx)
            return Response.html(html)

        return handler

    def _make_group_tree_view(self):
        """Build the group hierarchy tree view."""
        admin = self

        async def view(request):
            db = admin._get_db()
            # Load all groups with permission counts
            rows = await db.query(
                "SELECT g.id, g.name, g.parent_id, g.priority, "
                "COALESCE(pc.cnt, 0) AS perm_count, "
                "COALESCE(mc.cnt, 0) AS member_count "
                "FROM hyper_groups g "
                "LEFT JOIN (SELECT group_id, COUNT(*) AS cnt FROM hyper_group_permissions GROUP BY group_id) pc ON pc.group_id = g.id "
                "LEFT JOIN (SELECT group_id, COUNT(*) AS cnt FROM hyper_user_groups GROUP BY group_id) mc ON mc.group_id = g.id "
                "ORDER BY g.priority DESC, g.name"
            )
            cols = ["id", "name", "parent_id", "priority", "perm_count", "member_count"]
            groups = [
                dict(zip(cols, r)) if not isinstance(r, dict) else r for r in rows
            ]

            # Build tree structure
            by_id = {g["id"]: {**g, "children": []} for g in groups}
            roots = []
            for g in groups:
                node = by_id[g["id"]]
                parent = g["parent_id"]
                if parent and parent in by_id:
                    by_id[parent]["children"].append(node)
                else:
                    roots.append(node)

            # Flatten with depth for rendering
            flat = []

            def walk(node, depth):
                flat.append({**node, "depth": depth})
                for child in node["children"]:
                    walk(child, depth + 1)

            for root in roots:
                walk(root, 0)

            ctx = admin._base_context()
            ctx["title"] = "Group Hierarchy"
            ctx["tree"] = flat
            html = admin._render(TEMPLATE_GROUP_TREE, ctx)
            return Response.html(html)

        return view

    def _make_rbac_audit_view(self):
        """Build the RBAC audit log view."""
        admin = self

        async def view(request):
            db = admin._get_db()
            audit = RBACauditLog(db)
            entries = await audit.get_recent(limit=100)

            ctx = admin._base_context()
            ctx["title"] = "RBAC Audit Log"
            ctx["entries"] = entries
            html = admin._render(TEMPLATE_RBAC_AUDIT, ctx)
            return Response.html(html)

        return view

    def _make_rbac_policy_view(self):
        """Build the RBAC policy export/import page."""
        admin = self

        async def view(request):
            db = admin._get_db()
            checker = PermissionChecker(db)

            # Quick stats for the export panel
            stats = {}
            for table, key in [
                ("hyper_groups", "groups"),
                ("hyper_permissions", "permissions"),
                ("hyper_permission_rules", "rules"),
                ("hyper_field_permissions", "field_permissions"),
            ]:
                row = await db.query_one(f"SELECT COUNT(*) FROM {table}")
                stats[key] = (
                    int(row[0] if not isinstance(row, dict) else row["count"])
                    if row
                    else 0
                )

            ctx = admin._base_context()
            ctx["title"] = "RBAC Policy Export/Import"
            ctx["export_stats"] = stats
            ctx["import_result"] = None
            html = admin._render(TEMPLATE_RBAC_EXPORT, ctx)
            return Response.html(html)

        return view

    def _make_rbac_export_handler(self):
        """Build the RBAC policy JSON download handler."""
        admin = self

        async def view(request):
            db = admin._get_db()
            checker = PermissionChecker(db)
            policy = await checker.export_policy()
            resp = Response.json(policy)
            resp.headers["content-disposition"] = (
                'attachment; filename="rbac-policy.json"'
            )
            return resp

        return view

    def _make_rbac_import_handler(self):
        """Build the RBAC policy import handler (POST, multipart form)."""
        admin = self

        async def view(request):
            db = admin._get_db()
            checker = PermissionChecker(db)

            import_result = None
            try:
                body = request.body
                # dynamic-attr: probes an OPTIONAL pre-parsed form_data attribute some upload adapters attach; absent on the standard Request
                form_data = getattr(request, "form_data", None) or {}
                # Try to get uploaded file content
                file_data = None
                if hasattr(request, "files") and request.files:
                    file_obj = request.files.get("policy_file")
                    if file_obj:
                        file_data = (
                            file_obj
                            if isinstance(file_obj, (str, bytes))
                            # dynamic-attr: duck-typing an uploaded file — call .read() when file-like, else treat the object itself as the content
                            else getattr(file_obj, "read", lambda: file_obj)()
                        )
                elif isinstance(form_data, dict) and "policy_file" in form_data:
                    file_data = form_data["policy_file"]

                if file_data is None:
                    # Try parsing body as JSON directly
                    file_data = body

                if isinstance(file_data, bytes):
                    file_data = file_data.decode("utf-8")

                policy_data = fast_json_loads(file_data)
                clear = (
                    form_data.get("clear_existing") == "1"
                    if isinstance(form_data, dict)
                    else False
                )
                import_result = await checker.import_policy(
                    policy_data, clear_existing=clear
                )
            except (ValueError, RuntimeError) as e:
                import_result = {"imported": {}, "errors": [f"Invalid JSON: {e}"]}
            # blind-except: policy import processes arbitrary user-uploaded data; any failure is surfaced to the operator as an import error in the re-rendered page rather than a 500, and the request continues.
            except Exception as e:
                import_result = {"imported": {}, "errors": [str(e)]}

            # Re-render the policy page with results
            stats = {}
            for table, key in [
                ("hyper_groups", "groups"),
                ("hyper_permissions", "permissions"),
                ("hyper_permission_rules", "rules"),
                ("hyper_field_permissions", "field_permissions"),
            ]:
                row = await db.query_one(f"SELECT COUNT(*) FROM {table}")
                stats[key] = (
                    int(row[0] if not isinstance(row, dict) else row["count"])
                    if row
                    else 0
                )

            ctx = admin._base_context()
            ctx["title"] = "RBAC Policy Export/Import"
            ctx["export_stats"] = stats
            ctx["import_result"] = import_result
            html = admin._render(TEMPLATE_RBAC_EXPORT, ctx)
            return Response.html(html)

        return view

    def _make_rbac_dashboard_view(self):
        """Build the RBAC overview dashboard with stats and charts."""
        admin = self

        async def view(request):
            db = admin._get_db()
            stats = {}

            # Totals
            for table, key in [
                ("hyper_groups", "total_groups"),
                ("hyper_permissions", "total_permissions"),
                ("hyper_permission_rules", "total_rules"),
            ]:
                row = await db.query_one(f"SELECT COUNT(*) FROM {table}")
                stats[key] = (
                    int(row[0] if not isinstance(row, dict) else row["count"])
                    if row
                    else 0
                )

            row = await db.query_one(
                "SELECT COUNT(*) FROM hyper_users WHERE is_active = true"
            )
            stats["total_users"] = (
                int(row[0] if not isinstance(row, dict) else row["count"]) if row else 0
            )

            # Users per group
            rows = await db.query(
                "SELECT g.name, COUNT(ug.user_id) AS cnt "
                "FROM hyper_groups g "
                "LEFT JOIN hyper_user_groups ug ON g.id = ug.group_id "
                "GROUP BY g.id, g.name ORDER BY cnt DESC"
            )
            users_per_group = []
            max_count = 1
            for r in rows:
                if isinstance(r, dict):
                    d = {"name": r["name"], "count": int(r["cnt"]) if r["cnt"] else 0}
                else:
                    d = {"name": r[0], "count": int(r[1]) if r[1] else 0}
                max_count = max(max_count, d["count"])
                users_per_group.append(d)
            for d in users_per_group:
                d["pct"] = int(d["count"] / max_count * 100) if max_count > 0 else 0
            stats["users_per_group"] = users_per_group

            # Permission coverage by model
            rows = await db.query(
                "SELECT p.model_name, "
                "COUNT(DISTINCT p.id) AS perm_count, "
                "COUNT(DISTINCT gp.group_id) AS group_count, "
                "COUNT(DISTINCT up.user_id) AS user_count "
                "FROM hyper_permissions p "
                "LEFT JOIN hyper_group_permissions gp ON p.id = gp.permission_id "
                "LEFT JOIN hyper_user_permissions up ON p.id = up.permission_id "
                "GROUP BY p.model_name ORDER BY p.model_name"
            )
            coverage = []
            for r in rows:
                d = (
                    dict(
                        zip(
                            ["model_name", "perm_count", "group_count", "user_count"], r
                        )
                    )
                    if not isinstance(r, dict)
                    else r
                )
                for k in _COVERAGE_COUNT_KEYS:
                    d[k] = int(d[k]) if d[k] else 0
                coverage.append(d)
            stats["permission_coverage"] = coverage

            # Orphaned permissions (not assigned to any user or group)
            rows = await db.query(
                "SELECT p.codename, p.model_name FROM hyper_permissions p "
                "WHERE p.id NOT IN (SELECT permission_id FROM hyper_group_permissions) "
                "AND p.id NOT IN (SELECT permission_id FROM hyper_user_permissions) "
                "ORDER BY p.model_name, p.codename"
            )
            orphaned = []
            for r in rows:
                d = (
                    dict(zip(["codename", "model_name"], r))
                    if not isinstance(r, dict)
                    else r
                )
                orphaned.append(d)
            stats["orphaned_permissions"] = orphaned

            # Recent RBAC changes
            audit = RBACauditLog(db)
            stats["recent_changes"] = await audit.get_recent(limit=10)

            ctx = admin._base_context()
            ctx["title"] = "RBAC Overview"
            ctx["stats"] = stats
            html = admin._render(TEMPLATE_RBAC_DASHBOARD, ctx)
            return Response.html(html)

        return view

    async def _builtin_delete_action(self, config, request, selected_ids):
        """Built-in bulk delete action."""
        db = self._get_db()
        meta = config.model_class._meta
        for pk in selected_ids:
            delete_obj = await self._get_row(config, int(pk), request=request)
            for hook in config.delete_hooks:
                if _hook_param_count(hook) >= 3:
                    await hook(request, int(pk), delete_obj)
                else:
                    await hook(int(pk))
            await self._delete_row(config, pk, request=request)
        return f"Deleted {len(selected_ids)} {config.name}(s)"

    def _base_context(self, request=None):
        """Common template context."""
        ctx = {
            "admin_title": self.title,
            "prefix": self.prefix,
            "registered_models": [
                {"slug": cfg.slug, "name": cfg.name} for cfg in self._models.values()
            ],
        }
        if request is not None:
            ctx["csrf_token"] = self._generate_csrf_token(request)
            ctx["csrf_input"] = (
                f'<input type="hidden" name="_csrf_token" value="{ctx["csrf_token"]}">'
            )
            # dynamic-attr: csp_nonce is an OPTIONAL, middleware-supplied request attribute (nonce-based CSP middleware), not a declared Request field; when present the admin renders a matching nonce="..." on inline <script>/<style>
            csp_nonce = getattr(request, "csp_nonce", None)
            if csp_nonce:
                ctx["csp_nonce"] = csp_nonce
        return ctx

    def _generate_csrf_token(self, request) -> str:
        """Generate CSRF token from admin session cookie via HMAC."""
        cookie = request.cookies.get(ADMIN_SESSION_COOKIE, "")
        return sign_data(f"csrf:{cookie}", self._secret_key)

    def _verify_csrf_token(self, request) -> bool:
        """Verify CSRF token from form data against session cookie.

        Checks form body (_form._csrf_token) first, then X-CSRF-Token header.
        Returns True if auth not required, or if token matches session HMAC.
        """
        if not self._require_auth:
            return True

        form_token = None
        if hasattr(request, "_form") and request._form:
            tokens = request._form.get("_csrf_token", [])
            form_token = tokens[0] if tokens else None
        if not form_token:
            form_token = request.headers.get("x-csrf-token")
        if not form_token:
            return False
        expected = self._generate_csrf_token(request)
        return hmac.compare_digest(form_token, expected)

    async def _enforce_post_security(
        self, config, request, required_perm: str
    ) -> Response | None:
        """Check permission + CSRF for POST handlers. Returns error Response or None if OK.

        Centralizes the permission + CSRF check pattern used by all admin POST
        handlers (add, edit, delete, bulk action, save_list) to avoid duplication.
        """
        # Load user permissions from DB (lazy, cached per request)
        user = request._admin_user or self._check_auth(request)
        await self._load_user_permissions(user)

        perms = self._get_model_perms(config, request)
        if not perms.get(required_perm):
            return Response.error(403, f"Permission denied: {required_perm}")

        # Parse form body (needed for CSRF token extraction)
        await request.form()

        if not self._verify_csrf_token(request):
            return Response.error(403, "CSRF token invalid")

        return None  # All checks passed

    def _get_session_store(self):
        """Get or create the admin session store.

        SYNC-STORE-ONLY: the admin always uses the in-memory ``InMemorySessionStore``
        (never the async ``DatabaseSessionStore``). Every admin call site
        (``_check_auth``, ``_login_handler``, ``hash_password_hook``,
        ``_logout_handler``) therefore invokes ``store.get``/``create``/
        ``delete``/``invalidate_by_hash``/``invalidate_for_user`` synchronously
        without ``await``. That is correct for the in-memory store; wiring an
        async store here would require awaiting all of those sites.

        On first creation the store's ``invalidate_for_user`` is registered as a
        global RBAC session-invalidation hook, so an RBAC mutation elsewhere
        (e.g. ``PermissionChecker.remove_user_from_group``) drops this user's
        live admin sessions immediately rather than at expiry.
        """
        if self._session_store is None:
            self._session_store = InMemorySessionStore(max_age=ONE_DAY)
            # Real invalidation: RBAC de-escalation now reaches the admin store.
            register_session_invalidation_hook(self._session_store.invalidate_for_user)
        return self._session_store

    def _check_auth(self, request):
        """Check if the current request is from an authenticated staff user.

        Returns the user dict if authenticated, None otherwise.
        Verifies the session auth hash if present — if the user's password
        has changed since login, the session is silently invalidated.
        """
        if not self._require_auth:
            return {"username": "anonymous", "is_staff": True, "is_superuser": True}

        cookie = request.cookies.get(ADMIN_SESSION_COOKIE)
        if not cookie:
            return None
        session_id = verify_signed_data(cookie, self._secret_key)
        if not session_id:
            return None
        store = self._get_session_store()
        data = store.get(session_id)
        if not data:
            return None
        if not data.get("is_staff"):
            return None

        # SECURITY (live RBAC revocation): reject the session if this user's auth
        # epoch advanced since login. An RBAC mutation (remove-from-group,
        # perm revoke, is_staff/is_superuser flip, deactivation) bumps the epoch;
        # a stale stamp means the session's frozen privileges are no longer
        # authoritative, so drop it and force re-auth. This is the store-agnostic
        # backstop to the registered invalidate_for_user hook.
        user_id = data.get("user_id") or data.get("id")
        if data.get("_auth_epoch", 0) != get_auth_epoch(user_id):
            store.delete(session_id)
            return None

        # Verify session auth hash — detects password changes.
        # This is a lazy invalidation: we mark the session_id for async
        # verification in _check_auth_async. For sync _check_auth, we
        # store the session_id on the request for later async verification.
        request._admin_session_id = session_id
        return data

    async def _check_auth_with_hash_verify(self, request, user):
        """Verify session auth hash asynchronously (needs DB access).

        If the stored session auth hash doesn't match the user's current
        password_hash, the session is invalidated (password was changed).
        """
        stored_hash = user.get("_session_auth_hash", "")
        if not stored_hash:
            return True  # Session without a stored auth hash — allow through

        user_id = user.get("user_id") or user.get("id")
        if user_id is None:
            return True

        # Short-TTL cache: a recent successful verify lets us skip the DB round
        # trip for the same session. TTL is short so a password change is
        # detected within a few seconds.
        session_id = request._admin_session_id
        now = _time.monotonic()
        if session_id:
            expiry = self._hash_verify_cache.get(session_id)
            if expiry is not None:
                if expiry > now:
                    return True
                # Expired — drop the stale entry before re-verifying.
                self._hash_verify_cache.pop(session_id, None)

        try:
            db = self._get_db()
            row = await db.query_one(
                "SELECT password_hash FROM hyper_users WHERE id = $1", user_id
            )
        # blind-except: session hash re-verification fails CLOSED on any DB error — the handler denies the request (below) rather than skipping password-change invalidation, so no auth bypass is possible.
        except Exception:
            # DB error — fail CLOSED. Availability must not trump auth: allowing
            # the request through here would skip session-invalidation on a
            # changed password. Deny and let the caller redirect to login.
            _logger.warning(
                "admin: session-hash verify failed (DB error); denying request"
            )
            return False

        if row is None:
            return False  # User deleted

        current_pw_hash = row["password_hash"] if isinstance(row, dict) else row[0]
        expected = get_session_auth_hash(current_pw_hash, self._secret_key)
        if not hmac.compare_digest(stored_hash, expected):
            # Password changed — invalidate this session
            if session_id:
                self._hash_verify_cache.pop(session_id, None)
                store = self._get_session_store()
                store.delete(session_id)
            return False

        if session_id:
            self._hash_verify_cache[session_id] = now + _HASH_VERIFY_TTL
        return True

    async def _load_user_permissions(self, user):
        """Load user's permissions from DB and cache in user dict.

        Uses recursive CTE to resolve hierarchical group permissions —
        a user in "admin" (child of "editor" child of "viewer") gets all
        inherited permissions from the parent chain.
        Stores result as user["_permissions"] = set of codenames.
        Called lazily on first permission check per request.
        """
        if user is None or "_permissions" in user:
            return

        user_id = user.get("id") or user.get("user_id")
        if user_id is None:
            return

        try:
            db = self._get_db()
            # Direct user permissions
            direct = await db.query(
                "SELECT p.codename FROM hyper_permissions p "
                "JOIN hyper_user_permissions up ON up.permission_id = p.id "
                "WHERE up.user_id = $1",
                user_id,
            )
            # Group permissions WITH hierarchical inheritance via recursive CTE
            group = await db.query(
                "WITH RECURSIVE role_tree AS ("
                "  SELECT group_id AS id FROM hyper_user_groups WHERE user_id = $1 "
                "  UNION ALL "
                "  SELECT g.parent_id FROM hyper_groups g "
                "  JOIN role_tree rt ON g.id = rt.id "
                "  WHERE g.parent_id IS NOT NULL"
                ") "
                "SELECT DISTINCT p.codename FROM hyper_permissions p "
                "JOIN hyper_group_permissions gp ON gp.permission_id = p.id "
                "WHERE gp.group_id IN (SELECT id FROM role_tree)",
                user_id,
            )
            perms = set()
            for row in direct:
                perms.add(row["codename"] if isinstance(row, dict) else row[0])
            for row in group:
                perms.add(row["codename"] if isinstance(row, dict) else row[0])
            user["_permissions"] = perms
        # blind-except: permission tables are optional; if they don't exist yet the admin degrades to plain is_staff access control rather than 500ing.
        except Exception:
            # Permission tables may not exist yet — fall back to is_staff behavior
            pass

    async def _require_staff_or_redirect(self, request):
        """Returns a redirect Response to login if not staff, else None.

        Also verifies the session auth hash — if the user's password has
        been changed since login, the session is invalidated and they're
        redirected to the login page.
        """
        user = self._check_auth(request)
        if user is None:
            return Response.redirect(f"{self.prefix}/login/?next={request.path}")

        # Verify session auth hash (async — needs DB for current password_hash)
        if not await self._check_auth_with_hash_verify(request, user):
            return Response.redirect(f"{self.prefix}/login/?next={request.path}")

        # Per-request only. NEVER stash the acting user on instance state: under
        # free-threaded / concurrent execution a shared slot can be read by
        # another in-flight request (e.g. the escalation guard) and leak an
        # unrelated user's privileges. The request object is the single source
        # of truth for "who is acting".
        request._admin_user = user
        return None

    def _get_model_perms(self, config, request):
        """Get permission flags for the current user on a given model.

        Returns a dict with can_add, can_change, can_delete, can_view booleans.
        Superusers and anonymous (require_auth=False) get all permissions.
        Staff users are checked against the permission system when a
        PermissionChecker is configured, otherwise get full access (Django default).
        """
        user = request._admin_user
        if user is None:
            user = self._check_auth(request)

        # No auth required or superuser — full access
        if not self._require_auth or (user and user.get("is_superuser")):
            return {
                "can_add": True,
                "can_change": True,
                "can_delete": True,
                "can_view": True,
            }

        if user is None:
            return {
                "can_add": False,
                "can_change": False,
                "can_delete": False,
                "can_view": False,
            }

        # Check if we have cached permissions on the request
        cache_key = f"_perms_{config.slug}"
        # dynamic-attr: cache_key is a per-model name computed at runtime (_perms_<slug>); per-request permission memo stashed on the request under that dynamic key
        cached = getattr(request, cache_key, None)
        if cached is not None:
            return cached

        model_name = config.slug
        perms = {
            "can_add": self._user_has_perm(user, f"add_{model_name}"),
            "can_change": self._user_has_perm(user, f"change_{model_name}"),
            "can_delete": self._user_has_perm(user, f"delete_{model_name}"),
            "can_view": self._user_has_perm(user, f"view_{model_name}"),
        }

        # Cache on request to avoid re-checking within same request
        with contextlib.suppress(AttributeError, TypeError):
            # dynamic-attr: memoize the per-model permission dict on the request under the runtime-computed cache_key
            setattr(request, cache_key, perms)

        return perms

    async def _require_view_or_403(self, config, request) -> Response | None:
        """Enforce per-model ``view`` permission on GET handlers.

        ``_auth_wrap`` only gates on is_staff, so without this every staff user
        could read EVERY registered model (including the user table) via its
        list/detail/history/autocomplete endpoints. Mutations already funnel
        through ``_enforce_post_security``; this is the read-side counterpart.

        Loads the user's permission set first (otherwise ``_get_model_perms``
        falls back to "staff => all perms"), then returns a 403 Response when the
        user lacks ``view_<model>``, or None when access is allowed.
        """
        user = request._admin_user or self._check_auth(request)
        await self._load_user_permissions(user)
        perms = self._get_model_perms(config, request)
        if not perms.get("can_view"):
            return Response.error(403, "Permission denied: can_view")
        return None

    def _user_has_perm(self, user, perm_codename):
        """Check if a user dict has a given permission.

        Checks user's permission set if available (loaded by auth middleware),
        otherwise falls back to is_staff granting all permissions (Django default).
        """
        if user.get("is_superuser"):
            return True

        # Check explicit permissions if loaded (from hyper_user_permissions + hyper_group_permissions)
        user_perms = user.get("_permissions")
        if user_perms is not None:
            # Explicit permission set — check against it
            return perm_codename in user_perms

        # Fallback: staff users get all admin permissions
        # is_staff grants admin-site access
        # To enable granular RBAC, load permissions into user["_permissions"]
        # via the auth middleware or PermissionChecker.
        return bool(user.get("is_staff"))

    # ── Login / Logout ────────────────────────────────────────────────────

    async def _login_page(self, request):
        """Render the login form."""
        html = self.engine.render_string(
            TEMPLATE_LOGIN,
            self._login_context(request),
        )
        return Response.html(html)

    def _login_context(self, request, error: str = "") -> dict[str, str]:
        """Build context dict for the login template with CSRF token."""
        csrf_token = self._generate_csrf_token(request)
        return {
            "admin_title": self.title,
            "error": error,
            "csrf_token": csrf_token,
            "csrf_input": f'<input type="hidden" name="_csrf_token" value="{csrf_token}">',
        }

    async def _login_handler(self, request):
        """Process login form submission."""
        form_data = await request.form()
        raw_user = form_data.get("username", "")
        raw_pass = form_data.get("password", "")
        username = raw_user[0] if isinstance(raw_user, list) else raw_user
        password = raw_pass[0] if isinstance(raw_pass, list) else raw_pass

        # SECURITY (login CSRF) — INTENTIONALLY NOT ENFORCED here, unlike the
        # authenticated add/edit/delete POST handlers. The admin CSRF token is
        # HMAC-bound to the admin SESSION cookie (see _generate_csrf_token), but
        # login is pre-auth: there is no admin session cookie yet, so the token
        # degrades to a single constant shared by every anonymous client. A real
        # login-CSRF attacker can trivially fetch that constant from the public
        # login page, so verifying it stops no attack — while enforcing it WOULD
        # break legitimate programmatic logins that POST credentials directly
        # without first GETting the form (e.g. Session-based API clients and the
        # e2e suites). Meaningful login-CSRF protection would require a distinct
        # pre-session CSRF cookie, which this token scheme does not provide.
        # Post-login, every mutating admin endpoint IS CSRF-protected via
        # _enforce_post_security. Password entropy + the brute-force lockout are
        # the real controls on this endpoint. (self._verify_csrf_token still
        # exists and gates the authenticated handlers.)

        if not username or not password:
            html = self.engine.render_string(
                TEMPLATE_LOGIN,
                self._login_context(request, "Username and password are required."),
            )
            return Response.html(html, status=400)

        # Authenticate against hyper_users table
        db = self._get_db()
        checker = PermissionChecker(db)
        user_dict = await checker.authenticate(username, password)

        if user_dict is None:
            html = self.engine.render_string(
                TEMPLATE_LOGIN,
                self._login_context(request, "Invalid username or password."),
            )
            return Response.html(html, status=400)

        if not user_dict.get("is_staff"):
            html = self.engine.render_string(
                TEMPLATE_LOGIN,
                self._login_context(request, "You do not have staff access."),
            )
            return Response.html(html, status=403)

        # Create session with session auth hash for password change detection.
        # The hash is HMAC(secret, password_hash) — when the password changes,
        # the hash changes, and _check_auth silently invalidates the session.
        password_hash = user_dict.get("password_hash", "")
        session_data = {
            "user_id": user_dict["id"],
            "username": user_dict["username"],
            "is_staff": user_dict["is_staff"],
            "is_superuser": user_dict.get("is_superuser", False),
            # Stamp the current RBAC auth epoch. _check_auth rejects the session
            # once this user's epoch advances (a group/permission mutation), so a
            # de-escalated admin is forced to re-authenticate on their next
            # request even if the store-invalidation hook missed this session.
            "_auth_epoch": get_auth_epoch(user_dict["id"]),
        }
        if password_hash:
            session_data["_session_auth_hash"] = get_session_auth_hash(
                password_hash, self._secret_key
            )
        store = self._get_session_store()
        session_id = store.create(session_data)
        signed = sign_data(session_id, self._secret_key)

        next_url = request.GET.get("next", f"{self.prefix}/")
        # SECURITY: Prevent open redirect — only allow safe relative URLs
        if not is_safe_redirect_url(next_url):
            next_url = f"{self.prefix}/"
        resp = Response.redirect(next_url)
        resp.headers["set-cookie"] = (
            f"{ADMIN_SESSION_COOKIE}={signed}; Path=/; HttpOnly; SameSite=Lax; Max-Age={ONE_DAY}"
        )
        return resp

    async def _logout_handler(self, request):
        """Log out and redirect to login page."""
        cookie = request.cookies.get(ADMIN_SESSION_COOKIE)
        if cookie:
            session_id = verify_signed_data(cookie, self._secret_key)
            if session_id:
                store = self._get_session_store()
                store.delete(session_id)

        resp = Response.redirect(f"{self.prefix}/login/")
        resp.headers["set-cookie"] = f"{ADMIN_SESSION_COOKIE}=; Path=/; Max-Age=0"
        return resp

    @staticmethod
    def _check_config(
        model_name,
        field_names,
        *,
        list_display=None,
        search_fields=None,
        list_filter=None,
        list_editable=None,
        readonly_fields=None,
        fieldsets=None,
    ):
        """Validate admin configuration at register() time.

        Raises ValueError with a clear message if config references nonexistent fields
        or has invalid combinations (like list_editable on a readonly field).
        """
        errors = []

        # NOTE: list_display is intentionally NOT validated here. Entries may be
        # model fields, config-method names, or standalone callables resolved
        # from list_display_callables, none of which are visible from this
        # staticmethod. Unknown entries are handled at render time.

        if search_fields:
            for f in search_fields:
                if f not in field_names:
                    errors.append(
                        f"search_fields: '{f}' is not a field on {model_name}"
                    )

        if list_filter:
            for f in list_filter:
                if f not in field_names:
                    errors.append(f"list_filter: '{f}' is not a field on {model_name}")

        if list_editable:
            for f in list_editable:
                if f not in field_names:
                    errors.append(
                        f"list_editable: '{f}' is not a field on {model_name}"
                    )
                if readonly_fields and f in readonly_fields:
                    errors.append(
                        f"list_editable: '{f}' cannot be both editable and readonly on {model_name}"
                    )
                if list_display and f not in list_display:
                    errors.append(
                        f"list_editable: '{f}' must be in list_display to be editable on {model_name}"
                    )

        if readonly_fields:
            for f in readonly_fields:
                if f not in field_names:
                    errors.append(
                        f"readonly_fields: '{f}' is not a field on {model_name}"
                    )

        if fieldsets:
            for fs in fieldsets:
                for f in fs.fields:
                    # Skip virtual fields (start with _) — these are added dynamically
                    if f.startswith("_"):
                        continue
                    if f not in field_names:
                        errors.append(
                            f"fieldset '{fs.title or '(unnamed)'}': '{f}' is not a field on {model_name}"
                        )

        if errors:
            raise ValueError(
                f"Admin configuration errors for {model_name}:\n"
                + "\n".join(f"  - {e}" for e in errors)
            )

    def _auth_wrap(self, handler):
        """Wrap an async handler with auth checking. Redirects to login if not staff.

        Accepts **kwargs from router path params but doesn't forward them —
        admin handlers read path_params from request.path_params instead.
        """

        async def wrapped(request, **_kwargs):
            redirect = await self._require_staff_or_redirect(request)
            if redirect:
                return redirect
            return await handler(request)

        return wrapped

    def _auth_wrap_action(self, handler, slug, required_perm):
        """Auth wrapper for MUTATING custom ``model_action`` endpoints.

        Mirrors the built-in POST handlers: staff redirect first, then per-model
        permission + CSRF via ``_enforce_post_security``. When ``slug`` is not a
        registered admin model there is no per-model perm to enforce, so we fall
        back to CSRF-only on top of the staff gate (still closing the CSRF hole).
        Path params are read by the handler from ``request.path_params`` exactly
        as with ``_auth_wrap`` — the calling convention is unchanged.
        """

        async def wrapped(request, **_kwargs):
            redirect = await self._require_staff_or_redirect(request)
            if redirect:
                return redirect
            config = self._models.get(slug)
            if config is not None:
                error = await self._enforce_post_security(
                    config, request, required_perm
                )
                if error:
                    return error
            else:
                # No registered model for this slug — cannot resolve a per-model
                # permission, but still enforce CSRF so the mutating endpoint is
                # not cross-site forgeable (parse the body first so the token is
                # available to _verify_csrf_token).
                await request.form()
                if not self._verify_csrf_token(request):
                    return Response.error(403, "CSRF token invalid")
            return await handler(request)

        return wrapped

    def _get_m2m_descriptors(self, model_class) -> dict[str, ManyToManyField]:
        """Find all ManyToManyField descriptors on a model class."""
        m2m = {}
        for name, val in model_class.__dict__.items():
            if isinstance(val, ManyToManyField):
                m2m[name] = val
        return m2m

    async def _load_m2m_data(self, config, pk) -> dict[str, dict]:
        """Load M2M data for edit forms: available items + selected item IDs.

        Returns: {field_name: {"available": [(id, label), ...], "selected": [id, ...]}}
        """
        if not config.filter_horizontal:
            return {}

        db = self._get_db()
        m2m_descriptors = self._get_m2m_descriptors(config.model_class)
        result = {}

        for field_name in config.filter_horizontal:
            desc = m2m_descriptors.get(field_name)
            if desc is None:
                continue

            try:
                desc._ensure_target()
            except ValueError:
                continue
            target_table = desc._target_table_name
            junction_table = desc._junction_table
            source_col = desc._source_col
            target_col = desc._target_col
            target_pk = (
                desc._target_model._meta.pk_field if desc._target_model else "id"
            )

            # Find a display column on the target (name, title, username, label,
            # codename). The probe is up to 5 SELECTs, so cache the resolved
            # column per (slug, field) — it is stable for a table's schema.
            m2m_cache_key = (config.slug, field_name)
            display_col = self._m2m_display_col_cache.get(m2m_cache_key)
            if display_col is None:
                display_col = target_pk
                for candidate in ("name", "title", "username", "label", "codename"):
                    try:
                        await db.query_one(
                            f"SELECT {candidate} FROM {target_table} LIMIT 1"
                        )
                        display_col = candidate
                        break
                    # blind-except: probing which label column exists on the FK target; a failed candidate SELECT just tries the next one and falls back to the PK, so it must not propagate.
                    except Exception:
                        continue
                self._m2m_display_col_cache[m2m_cache_key] = display_col

            # Load all available items
            rows = await db.query(
                f"SELECT {target_pk}, {display_col} FROM {target_table} ORDER BY {display_col} LIMIT 500"
            )
            available = []
            for r in rows:
                if isinstance(r, dict):
                    vals = list(r.values())
                    available.append(
                        (vals[0], str(vals[1]) if len(vals) > 1 else str(vals[0]))
                    )
                else:
                    available.append((r[0], str(r[1]) if len(r) > 1 else str(r[0])))

            # Load selected IDs for this object
            selected_ids = []
            if pk is not None:
                sel_rows = await db.query(
                    f"SELECT {target_col} FROM {junction_table} WHERE {source_col} = $1",
                    int(pk),
                )
                for r in sel_rows:
                    val = list(r.values())[0] if isinstance(r, dict) else r[0]
                    selected_ids.append(val)

            result[field_name] = {
                "available": available,
                "selected": selected_ids,
                "label": field_name.replace("_", " ").title(),
            }

        return result

    async def _save_m2m_data(self, config, pk, form_data) -> None:
        """Save M2M selections from form data to junction tables.

        The whole set of DELETE + INSERTs runs inside a single transaction so a
        crash mid-way can never leave a half-written selection (the DELETE and
        re-inserts commit together or not at all). Each field's re-insert is a
        single multi-VALUES statement instead of N round trips.
        """
        if not config.filter_horizontal:
            return

        db = self._get_db()
        m2m_descriptors = self._get_m2m_descriptors(config.model_class)
        source_pk = int(pk)

        async with db.transaction():
            for field_name in config.filter_horizontal:
                desc = m2m_descriptors.get(field_name)
                if desc is None:
                    continue

                desc._ensure_target()
                junction_table = desc._junction_table
                source_col = desc._source_col
                target_col = desc._target_col

                # Get selected IDs from form (multi-value field)
                raw = form_data.get(f"m2m_{field_name}")
                if raw is None:
                    selected = []
                elif isinstance(raw, list):
                    selected = [int(v) for v in raw if v]
                else:
                    selected = [int(raw)] if raw else []

                # Clear existing and insert new (atomic set)
                await db.execute(
                    f"DELETE FROM {junction_table} WHERE {source_col} = $1", source_pk
                )
                if selected:
                    # Single multi-VALUES insert: ($1,$2),($1,$3),...
                    values_sql = ", ".join(
                        f"($1, ${i + 2})" for i in range(len(selected))
                    )
                    await db.execute(
                        f"INSERT INTO {junction_table} ({source_col}, {target_col}) "
                        f"VALUES {values_sql}",
                        source_pk,
                        *selected,
                    )

    def _post_save_redirect(self, config, request, pk, action, msg):
        """Compute redirect URL after add/change/delete based on response_* config.

        action: "add" | "change" | "delete"
        Response config values: "list" (default), "continue" (stay on edit),
        "add" (add another), or callable(request, pk) → URL string.
        """
        response_config = {
            "add": config.response_add,
            "change": config.response_change,
            "delete": config.response_delete,
        }.get(action, "list")

        if callable(response_config):
            url = response_config(request, pk)
            return Response.redirect(url)
        if response_config == "continue" and action != "delete":
            return Response.redirect(f"{self.prefix}/{config.slug}/{pk}/?msg={msg}")
        if response_config == "add" and action != "delete":
            return Response.redirect(f"{self.prefix}/{config.slug}/add/?msg={msg}")
        # Default: back to list
        return Response.redirect(f"{self.prefix}/{config.slug}/?msg={msg}")

    def _resolve_template(self, config, view_type, default_source):
        """Resolve a template with 3-level cascade: model → admin → built-in default.

        Checks for filesystem templates in order:
            1. {templates_dir}/{slug}/{view_type}.html     (per-model)
            2. {templates_dir}/admin/{view_type}.html      (site-wide admin)
            3. Built-in inline template string              (default)

        Also checks the explicit override on ModelConfig (list_template, etc.)

        The resolution is cached per (slug, view_type). File-backed results are
        revalidated by stat/mtime, so an edited template is picked up on the next
        request without paying the two exists() stats + read_text on every render
        (the common built-in-default case pays nothing after the first call).
        """
        cache_key = (config.slug if config else None, view_type)
        cached = self._template_cache.get(cache_key)
        if cached is not None:
            cpath, cmtime, csource = cached
            if cpath is None:
                # Built-in default or an inline override string — no file to
                # revalidate; the source is immutable for the process lifetime.
                return csource
            try:
                if cpath.stat().st_mtime == cmtime:
                    return csource
            except OSError:
                pass  # File vanished — fall through and re-resolve the cascade.

        def _store(path, source):
            mtime = None
            if path is not None:
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    path = None
            self._template_cache[cache_key] = (path, mtime, source)
            return source

        # Check explicit override first
        # dynamic-attr: template override attr name is composed from view_type at runtime (<view_type>_template) on the per-model config
        override = getattr(config, f"{view_type}_template", None) if config else None
        if override:
            # Could be a file path or a template string
            template_path = (
                Path(self.app.templates_dir or "templates") / override
                if self.app.templates_dir
                else None
            )
            if template_path and template_path.exists():
                return _store(template_path, template_path.read_text())
            # Treat as inline template string
            return _store(None, override)

        # 3-level filesystem cascade
        templates_dir = self.app.templates_dir or "templates"
        if config:
            paths = [
                Path(templates_dir) / config.slug / f"{view_type}.html",
                Path(templates_dir) / "admin" / f"{view_type}.html",
            ]
            for p in paths:
                if p.exists():
                    return _store(p, p.read_text())

        return _store(None, default_source)

    def _render(self, template_source, context, config=None):
        """Render a template string with context + media injection."""
        ctx = self._base_context()
        ctx.update(context)
        # Inject per-model media
        media_html = ""
        if config:
            for css in config.media_css:
                media_html += f'<link rel="stylesheet" href="{css}">\n'
            for js in config.media_js:
                media_html += f'<script src="{js}"></script>\n'
        ctx["extra_media"] = media_html
        return self.engine.render_string(template_source, ctx)

    # ── Dashboard ─────────────────────────────────────────────────────────

    async def _dashboard(self, request):
        redirect = await self._require_staff_or_redirect(request)
        if redirect:
            return redirect

        models_info = []
        for cfg in self._models.values():
            models_info.append(
                {
                    "slug": cfg.slug,
                    "name": cfg.name,
                    "field_count": cfg.field_count,
                }
            )

        # Load recent activity from audit log (auto-create table if missing)
        recent_activity = []
        try:
            db = self._get_db()
            audit = AuditLog(db)
            await audit.ensure_table()
            recent_activity = await audit.get_recent(limit=10)
        # blind-except: the dashboard's recent-activity widget is optional; if the audit table can't be created/read the page renders without it.
        except Exception:
            pass  # Audit table creation may fail in test environments

        ctx = self._base_context()
        ctx["title"] = "Dashboard"
        ctx["models"] = models_info
        ctx["admin_user"] = request._admin_user
        ctx["recent_activity"] = recent_activity
        ctx["has_auth_models"] = self._has_auth_models
        ctx["has_cache_dashboard"] = self._has_cache_dashboard
        html = self._render(TEMPLATE_DASHBOARD, ctx)
        return Response.html(html)

    # ── List View ─────────────────────────────────────────────────────────

    async def _build_list_context(self, config, request):
        """Build the template context for list views (shared by full + partial)."""
        # Per-model view permission — is_staff alone must not grant read access
        # to every model. Covers both the full list view and the HTMX partial.
        denied = await self._require_view_or_403(config, request)
        if denied is not None:
            return denied

        db = self._get_db()
        meta = config.model_class._meta

        try:
            page = max(1, int(request.GET.get("page", "1")))
        except ValueError, TypeError:
            page = 1
        sort_field = request.GET.get("sort", config.ordering or meta.pk_field)
        # SECURITY: Validate sort_field against model columns to prevent SQL injection
        if sort_field.lstrip("-") not in meta.column_names:
            sort_field = meta.pk_field
        sort_dir = request.GET.get("dir", "asc")
        # DoS: cap search length. Each searchable field runs `::text ILIKE '%q%'`
        # (a full scan), so an unbounded `q` is a cheap way to force expensive
        # queries. Mirror the REST layer's 200-char cap.
        search_query = request.GET.get("q", "")[:_ADMIN_MAX_SEARCH_LENGTH]

        if sort_field.startswith("-"):
            sort_field = sort_field[1:]
            if sort_dir == "asc":
                sort_dir = "desc"

        conditions = []
        params = []

        # Dynamic queryset filtering via get_queryset(request)
        if config.get_queryset is not None:
            qs_filter = await config.get_queryset(request)
            if qs_filter is not None:
                for col, val in qs_filter.items():
                    # SECURITY: validate column name against model columns
                    if col in meta.column_names:
                        params.append(val)
                        conditions.append(f"{col} = ${len(params)}")

        if search_query and config.searchable_fields:
            search_conds = []
            for sf in config.searchable_fields:
                params.append(f"%{search_query}%")
                search_conds.append(f"{sf}::text ILIKE ${len(params)}")
            conditions.append(f"({' OR '.join(search_conds)})")

        active_filters = {}
        for ff in config.list_filter:
            fval = request.GET.get(f"filter_{ff}", "")
            active_filters[ff] = fval
            if fval:
                params.append(fval)
                conditions.append(f"{ff}::text = ${len(params)}")

        # Auto-scope to current tenant if model uses TenantMixin
        inject_tenant_condition(config.model_class, conditions, params)

        # Date hierarchy filtering. dh_* come straight off the query string, so
        # validate before int(): a non-numeric ?dh_year=x must be a 400, not an
        # uncaught ValueError bubbling to a 500.
        date_hierarchy_data = None
        if config.date_hierarchy:
            dh_field = config.date_hierarchy
            try:
                dh_year = _optional_int(request.GET.get("dh_year", ""))
                dh_month = _optional_int(request.GET.get("dh_month", ""))
                dh_day = _optional_int(request.GET.get("dh_day", ""))
            except ValueError:
                return Response.html(
                    "Invalid date hierarchy parameter — "
                    "dh_year/dh_month/dh_day must be integers.",
                    status=400,
                )

            if dh_year:
                params.append(dh_year)
                conditions.append(f"EXTRACT(YEAR FROM {dh_field}) = ${len(params)}")
            if dh_month:
                params.append(dh_month)
                conditions.append(f"EXTRACT(MONTH FROM {dh_field}) = ${len(params)}")
            if dh_day:
                params.append(dh_day)
                conditions.append(f"EXTRACT(DAY FROM {dh_field}) = ${len(params)}")

            # Build navigation data (tenant-scoped via parameterized queries)
            if dh_year and dh_month:
                # Show days in this month
                dh_conds: list[str] = [
                    f"EXTRACT(YEAR FROM {dh_field}) = $1",
                    f"EXTRACT(MONTH FROM {dh_field}) = $2",
                ]
                dh_params: list[object] = [dh_year, dh_month]
                inject_tenant_condition(config.model_class, dh_conds, dh_params)
                day_sql = (
                    f"SELECT DISTINCT EXTRACT(DAY FROM {dh_field})::int AS d "
                    f"FROM {meta.table} WHERE {' AND '.join(dh_conds)} ORDER BY d"
                )
                day_rows = await db.query(day_sql, *dh_params)
                days = [
                    r[0] if not isinstance(r, dict) else list(r.values())[0]
                    for r in day_rows
                ]
                date_hierarchy_data = {
                    "level": "day",
                    "year": dh_year,
                    "month": dh_month,
                    "items": [{"value": d, "label": str(d)} for d in days],
                    "active_day": dh_day,
                }
            elif dh_year:
                # Show months in this year
                dhm_conds: list[str] = [f"EXTRACT(YEAR FROM {dh_field}) = $1"]
                dhm_params: list[object] = [dh_year]
                inject_tenant_condition(config.model_class, dhm_conds, dhm_params)
                month_sql = (
                    f"SELECT DISTINCT EXTRACT(MONTH FROM {dh_field})::int AS m "
                    f"FROM {meta.table} WHERE {' AND '.join(dhm_conds)} ORDER BY m"
                )
                month_rows = await db.query(month_sql, *dhm_params)
                months = [
                    r[0] if not isinstance(r, dict) else list(r.values())[0]
                    for r in month_rows
                ]
                month_names = [
                    "",
                    "Jan",
                    "Feb",
                    "Mar",
                    "Apr",
                    "May",
                    "Jun",
                    "Jul",
                    "Aug",
                    "Sep",
                    "Oct",
                    "Nov",
                    "Dec",
                ]
                date_hierarchy_data = {
                    "level": "month",
                    "year": dh_year,
                    "items": [{"value": m, "label": month_names[m]} for m in months],
                    "active_month": dh_month,
                }
            else:
                # Show years
                dhy_conds: list[str] = [f"{dh_field} IS NOT NULL"]
                dhy_params: list[object] = []
                inject_tenant_condition(config.model_class, dhy_conds, dhy_params)
                year_sql = (
                    f"SELECT DISTINCT EXTRACT(YEAR FROM {dh_field})::int AS y "
                    f"FROM {meta.table} WHERE {' AND '.join(dhy_conds)} ORDER BY y"
                )
                year_rows = await db.query(year_sql, *dhy_params)
                years = [
                    r[0] if not isinstance(r, dict) else list(r.values())[0]
                    for r in year_rows
                ]
                date_hierarchy_data = {
                    "level": "year",
                    "items": [{"value": y, "label": str(y)} for y in years],
                }

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        # Only pay for COUNT(*) when the template will actually show a total.
        # With show_full_result_count=False the count is never rendered, so the
        # scan is pure waste on large tables.
        if config.show_full_result_count:
            total = await db.query_val(
                f"SELECT COUNT(*) FROM {meta.table} {where_clause}", *params
            )
        else:
            total = None

        # DoS: a huge ?page (e.g. page=1000000000) turns into an enormous OFFSET
        # that scans and discards millions of rows. When we know the real count,
        # clamp the page to the last page that can hold rows — mirroring the
        # paginator's num_pages guard — so OFFSET can never run past the data.
        if total is not None:
            max_page = max(1, (int(total) + config.per_page - 1) // config.per_page)
            if page > max_page:
                page = max_page

        order = "ASC" if sort_dir == "asc" else "DESC"
        offset = (page - 1) * config.per_page
        data_sql = (
            f"SELECT * FROM {meta.table} {where_clause} "
            f"ORDER BY {sort_field} {order} "
            f"LIMIT {config.per_page} OFFSET {offset}"
        )
        rows_raw = await db.query(data_sql, *params)

        # Dynamic columns via get_list_display(request)
        if config.get_list_display is not None:
            dynamic_cols = config.get_list_display(request)
            field_by_name = config._field_by_name
            columns = []
            for col_name in dynamic_cols:
                af = field_by_name.get(col_name)
                if af:
                    columns.append(
                        {"name": col_name, "label": af.label, "is_callable": False}
                    )
                elif col_name in (config.list_display_callables or {}):
                    columns.append(
                        {
                            "name": col_name,
                            "label": col_name.replace("_", " ").title(),
                            "is_callable": True,
                        }
                    )
        else:
            columns = config.display_columns
        all_col_names = meta.column_names
        editable_set = set(config.list_editable)
        field_by_name = config._field_by_name

        # Pre-load FK display names for all FK columns in list_display.
        # The display column choice (name/title/username/codename/label) is
        # cached on the ModelConfig per FK column after the first request,
        # so subsequent requests skip the up-to-5 SELECT ... LIMIT 1 probes.
        fk_lookups = {}  # {col_name: {pk_int: "display_name"}}
        fk_display_col_cache = config._fk_display_col_cache
        for col in columns:
            af = field_by_name.get(col["name"])
            if af is None or not af.foreign_key:
                continue
            fk_table = af.foreign_key
            # Collect all FK values from the result set
            fk_ids = set()
            for r in rows_raw:
                rd = dict(zip(all_col_names, r)) if not isinstance(r, dict) else r
                v = rd.get(col["name"])
                if v is not None:
                    fk_ids.add(int(v))
            if not fk_ids:
                continue
            # Resolved display column (cached per FK column on the config)
            display_col = fk_display_col_cache.get(col["name"])
            if display_col is None:
                display_col = "id"
                for candidate in _LABEL_CANDIDATES:
                    try:
                        test_row = await db.query_one(
                            f"SELECT {candidate} FROM {fk_table} LIMIT 1"
                        )
                        if test_row is not None:
                            display_col = candidate
                            break
                    # blind-except: probing which label column exists on the FK target; a failed candidate SELECT just tries the next one and falls back to "id", so it must not propagate.
                    except Exception:
                        continue
                fk_display_col_cache[col["name"]] = display_col
            # Batch load names for the FK ids in this page
            placeholders = ", ".join(f"${i + 1}" for i in range(len(fk_ids)))
            fk_rows = await db.query(
                f"SELECT id, {display_col} FROM {fk_table} WHERE id IN ({placeholders})",
                *list(fk_ids),
            )
            lookup = {}
            for fr in fk_rows:
                fk_pk = fr["id"] if isinstance(fr, dict) else fr[0]
                fk_name = fr[display_col] if isinstance(fr, dict) else fr[1]
                lookup[fk_pk] = f"{fk_name}"
            fk_lookups[col["name"]] = lookup

        rows = []
        for row in rows_raw:
            row_dict = (
                dict(zip(all_col_names, row)) if not isinstance(row, dict) else row
            )
            pk_val = row_dict.get(meta.pk_field)
            cells = []
            values = []
            for col in columns:
                if col["is_callable"]:
                    fn = config.list_display_callables.get(col["name"])
                    raw_val = fn(row_dict) if fn else ""
                else:
                    raw_val = row_dict.get(col["name"], "")

                # Build display value — resolve FK names
                if isinstance(raw_val, bool):
                    display = "✓" if raw_val else "✗"
                elif raw_val is None:
                    display = config.empty_value_display
                elif col["name"] in fk_lookups and raw_val is not None:
                    display = fk_lookups[col["name"]].get(int(raw_val), str(raw_val))
                else:
                    display = str(raw_val)
                    if len(display) > 100:
                        display = display[:100] + "..."
                values.append(display)

                # Build cell with editable info
                is_editable = col["name"] in editable_set and not col["is_callable"]
                widget = "text"
                if is_editable:
                    af = field_by_name.get(col["name"])
                    if af is not None:
                        widget = af.widget
                # Determine if this cell should be a link to the edit form
                if config.list_display_links is None:
                    # Default sentinel (not set) — first column links
                    is_link = col == columns[0]
                elif config.list_display_links is False:
                    # Explicitly disabled — no links
                    is_link = False
                else:
                    is_link = col["name"] in config.list_display_links

                cells.append(
                    {
                        "display": display,
                        "value": "" if raw_val is None else raw_val,
                        "raw_value": raw_val,
                        "editable": is_editable,
                        "field_name": col["name"],
                        "widget": widget,
                        "is_link": is_link,
                    }
                )
            rows.append({"pk": pk_val, "values": values, "cells": cells})

        if total is not None:
            total_pages = max(1, (total + config.per_page - 1) // config.per_page)
        else:
            # No full count computed — infer "has next page" from whether this
            # page came back full, so pagination still works without COUNT(*).
            total_pages = page + 1 if len(rows_raw) >= config.per_page else page
        page_range = list(range(max(1, page - 3), min(total_pages + 1, page + 4)))

        filters_data = []
        now = _time.monotonic()
        for ff in config.list_filter:
            af = field_by_name.get(ff)
            if af is not None:
                dist_conds: list[str] = [f"{ff} IS NOT NULL"]
                dist_params: list[object] = []
                inject_tenant_condition(config.model_class, dist_conds, dist_params)
                dist_where = " AND ".join(dist_conds)
                distinct_sql = f"SELECT DISTINCT {ff}::text FROM {meta.table} WHERE {dist_where} ORDER BY 1 LIMIT 50"
                # Short-TTL cache. The key includes the resolved params, which
                # carry the tenant scope, so cached options never leak across
                # tenants. This collapses one DISTINCT scan per filter per render
                # to at most one per TTL window.
                cache_key = (distinct_sql, tuple(dist_params))
                cached = self._filter_distinct_cache.get(cache_key)
                if cached is not None and cached[0] > now:
                    options = cached[1]
                else:
                    distinct_rows = await db.query(distinct_sql, *dist_params)
                    options = [
                        {
                            "value": list(r.values())[0]
                            if isinstance(r, dict)
                            else r[0],
                            "label": list(r.values())[0]
                            if isinstance(r, dict)
                            else r[0],
                        }
                        for r in distinct_rows
                    ]
                    self._filter_distinct_cache[cache_key] = (
                        now + _FILTER_DISTINCT_TTL,
                        options,
                    )
                filters_data.append(
                    {
                        "name": ff,
                        "label": af.label,
                        "options": options,
                        "active_value": active_filters.get(ff, ""),
                    }
                )

        # Build permission context
        perms = self._get_model_perms(config, request)

        # The list template renders `total` unconditionally. When the full
        # COUNT(*) is gated off (show_full_result_count=False) we never scanned
        # the table, so display the current page's row count instead of None.
        display_total = total if total is not None else len(rows_raw)

        ctx = self._base_context()
        ctx.update(
            {
                "title": config.name,
                "model_name": config.name,
                "slug": config.slug,
                "columns": columns,
                "rows": rows,
                "total": display_total,
                "page": page,
                "total_pages": total_pages,
                "page_range": page_range,
                "sort_field": sort_field,
                "sort_dir": sort_dir,
                "search_query": search_query,
                "message": request.GET.get("msg", ""),
                "error_message": request.GET.get("err", ""),
                "actions": [{"name": a.name, "label": a.label} for a in config.actions],
                "filters": filters_data,
                "active_filters": active_filters,
                "list_editable": bool(config.list_editable),
                "perms": perms,
                "date_hierarchy": date_hierarchy_data,
                "show_full_result_count": config.show_full_result_count,
                "sortable_by": config.sortable_by,
            }
        )
        return ctx

    def _make_list_view(self, config):
        async def list_view(request):
            ctx = await self._build_list_context(config, request)
            if isinstance(ctx, Response):
                return ctx  # e.g. 400 for a bad date-hierarchy param
            template = self._resolve_template(config, "list", TEMPLATE_LIST)
            html = self._render(template, ctx, config)
            return Response.html(html)

        return list_view

    def _make_list_action_handler(self, config):
        """Handle bulk actions from the list view POST."""

        async def action_handler(request):
            error = await self._enforce_post_security(config, request, "can_delete")
            if error:
                return error

            form_data = await request.form()
            action_name = form_data.get("_action", "")
            selected = (
                form_data.getlist("_selected") if hasattr(form_data, "getlist") else []
            )

            # Fallback for form data that returns single values
            if not selected and "_selected" in form_data:
                val = form_data["_selected"]
                selected = val if isinstance(val, list) else [val]

            if not action_name:
                return Response.redirect(
                    f"{self.prefix}/{config.slug}/?err=No+action+selected"
                )
            if not selected:
                return Response.redirect(
                    f"{self.prefix}/{config.slug}/?err=No+items+selected"
                )

            action = next((a for a in config.actions if a.name == action_name), None)
            if not action:
                return Response.redirect(
                    f"{self.prefix}/{config.slug}/?err=Unknown+action"
                )

            msg = await action.handler(config, request, selected)
            safe_msg = msg.replace(" ", "+") if msg else "Action+completed"
            return Response.redirect(f"{self.prefix}/{config.slug}/?msg={safe_msg}")

        return action_handler

    # ── List Editable Save Handler ───────────────────────────────────────

    def _make_save_list_handler(self, config):
        """Handle POST from list view to save inline-edited field values.

        Form data contains fields named "{field_name}_{pk}" with edited values.
        Updates each row with its changed values in a batch.
        """

        async def save_list_handler(request):
            error = await self._enforce_post_security(config, request, "can_change")
            if error:
                return error

            form_data = await request.form()
            db = self._get_db()
            meta = config.model_class._meta

            # Collect changes per PK
            changes = {}  # {pk: {field: value, ...}}
            for key, value in form_data.items():
                if key.startswith("_"):
                    continue
                # Parse field_name_pk pattern
                for editable_field in config.list_editable:
                    prefix = f"{editable_field}_"
                    if key.startswith(prefix):
                        pk_str = key[len(prefix) :]
                        try:
                            pk = int(pk_str)
                        except ValueError:
                            continue
                        if pk not in changes:
                            changes[pk] = {}
                        # Coerce the value to the right type
                        af = next(
                            (f for f in config.fields if f.name == editable_field), None
                        )
                        if af:
                            try:
                                coerced = _coerce_value(value, af.python_type)
                            except ValueError, TypeError:
                                coerced = value
                            changes[pk][editable_field] = coerced
                        else:
                            changes[pk][editable_field] = value

            # Handle checkbox fields (unchecked = not in form data)
            for editable_field in config.list_editable:
                af = next((f for f in config.fields if f.name == editable_field), None)
                if af and af.python_type is bool:
                    # For bool fields, any PK in the form that doesn't have this field
                    # means the checkbox was unchecked
                    for pk in changes:
                        if editable_field not in changes[pk]:
                            changes[pk][editable_field] = False

            # Batch update each row (tenant-scoped via _update_row)
            updated = 0
            for pk, field_values in changes.items():
                if not field_values:
                    continue
                row_values = dict(field_values)
                row_values[meta.pk_field] = pk
                await self._update_row(config, pk, row_values, request=request)
                updated += 1

                # Audit log
                await self._audit_log(
                    "change",
                    config,
                    pk,
                    str(field_values),
                    changes=field_values,
                    request=request,
                )

            msg = f"Updated+{updated}+row{'s' if updated != 1 else ''}"
            return Response.redirect(f"{self.prefix}/{config.slug}/?msg={msg}")

        return save_list_handler

    # ── Partial View (HTMX) ──────────────────────────────────────────────

    def _make_partial_view(self, config):
        """Return just the result table HTML for HTMX partial swaps."""

        async def partial_view(request):
            # Reuse the list view's data-fetching logic
            ctx = await self._build_list_context(config, request)
            if isinstance(ctx, Response):
                return ctx  # e.g. 400 for a bad date-hierarchy param
            html = self.engine.render_string(TEMPLATE_LIST_PARTIAL, ctx)
            return Response.html(html)

        return partial_view

    # ── Validate Handler (HTMX) ──────────────────────────────────────────

    def _make_validate_handler(self, config):
        """Field-level validation via HTMX — returns error or valid HTML."""

        async def validate_handler(request):
            form_data = await request.form()
            field_name = form_data.get("_field", "")
            field_value = form_data.get(field_name, "")

            af = next((f for f in config.form_fields if f.name == field_name), None)
            if not af:
                return Response.html("")

            # Type coercion check
            if field_value == "" and af.required:
                html = self.engine.render_string(
                    TEMPLATE_FIELD_ERROR, {"error": f"{af.label} is required"}
                )
                return Response.html(html)

            if field_value != "":
                try:
                    _coerce_value(field_value, af.python_type)
                    html = self.engine.render_string(TEMPLATE_FIELD_VALID, {})
                    return Response.html(html)
                except (ValueError, TypeError) as e:
                    html = self.engine.render_string(
                        TEMPLATE_FIELD_ERROR, {"error": str(e)}
                    )
                    return Response.html(html)

            return Response.html("")

        return validate_handler

    # ── FK Autocomplete (HTMX) ───────────────────────────────────────────

    def _make_autocomplete_handler(self, config):
        """Return autocomplete search results for FK fields."""

        async def autocomplete_handler(request):
            denied = await self._require_view_or_403(config, request)
            if denied is not None:
                return denied
            field_name = request.GET.get("field", "")
            query = request.GET.get("q", "")

            # Require non-empty query to prevent full-table enumeration
            if not query or len(query.strip()) < 1:
                return Response.html("")

            # Support inline FK fields via explicit fk_table param
            fk_table_override = request.GET.get("fk_table", "")

            # Validate fk_table against known auth tables (prevent injection)
            _VALID_FK_TABLES = {
                "hyper_users",
                "hyper_groups",
                "hyper_permissions",
                "hyper_user_groups",
                "hyper_group_permissions",
                "hyper_user_permissions",
                "hyper_object_permissions",
                "hyper_permission_rules",
                "hyper_field_permissions",
            }
            if fk_table_override and fk_table_override not in _VALID_FK_TABLES:
                # Also allow any registered model table
                known_tables = {
                    m.model_class._meta.table for m in self._models.values()
                }
                if fk_table_override not in known_tables:
                    return Response.html("")

            # Find the FK field — check parent model fields first, then inline fields
            af = next((f for f in config.fields if f.name == field_name), None)
            if af and not af.foreign_key and not fk_table_override:
                return Response.html("")
            if not af and not fk_table_override:
                # Check inline model fields
                for inline in config.inlines:
                    inline_fields = _get_inline_fields(inline)
                    af = next((f for f in inline_fields if f.name == field_name), None)
                    if af and af.foreign_key:
                        break
                else:
                    if not fk_table_override:
                        return Response.html("")

            # Look up the related table
            related_table = fk_table_override or (af.foreign_key if af else "")

            related_model = _get_model_by_table(related_table)

            db = self._get_db()

            if related_model:
                related_meta = related_model._meta
                pk_col = related_meta.pk_field
                # Find a display column (name, title, username, or first str field)
                display_col = pk_col
                for col_name in _FK_DISPLAY_COLUMNS:
                    if col_name in related_meta.fields:
                        display_col = col_name
                        break
                if display_col == pk_col:
                    # Use first non-PK field
                    for col_name, fmeta in related_meta.fields.items():
                        if not fmeta.primary_key and not fmeta.auto:
                            display_col = col_name
                            break

                # Build conditions for autocomplete (tenant + search)
                ac_conditions: list[str] = []
                ac_params: list[object] = []
                inject_tenant_condition(related_model, ac_conditions, ac_params)
                if query:
                    ac_params.append(f"%{query}%")
                    ac_conditions.append(f"{display_col}::text ILIKE ${len(ac_params)}")
                ac_where = (
                    f"WHERE {' AND '.join(ac_conditions)}" if ac_conditions else ""
                )
                sql = (
                    f"SELECT {pk_col}, {display_col} FROM {related_table} "
                    f"{ac_where} ORDER BY {display_col} LIMIT 20"
                )
                rows = await db.query(sql, *ac_params)
            else:
                # Fallback: query the table directly without model
                if query:
                    sql = f"SELECT id, id::text FROM {related_table} WHERE id::text LIKE $1 LIMIT 20"
                    rows = await db.query(sql, f"%{query}%")
                else:
                    sql = f"SELECT id, id::text FROM {related_table} LIMIT 20"
                    rows = await db.query(sql)

            # Render as clickable options
            if not rows:
                return Response.html(
                    '<div style="padding:0.5rem;color:var(--muted);font-size:0.8125rem;">No results</div>'
                )

            html_parts = []
            for row in rows:
                pk_val = row[0] if not isinstance(row, dict) else list(row.values())[0]
                display = row[1] if not isinstance(row, dict) else list(row.values())[1]
                # Escape pk_val too — a string/UUID PK containing " or < would
                # otherwise break out of the data-pk attribute (integer PKs are
                # safe, but the PK type is not guaranteed).
                pk_safe = _escape_html(str(pk_val))
                html_parts.append(
                    f'<div data-pk="{pk_safe}" style="padding:0.375rem 0.5rem;cursor:pointer;font-size:0.875rem;" '
                    f"onmouseover=\"this.style.background='var(--hover)'\" "
                    f"onmouseout=\"this.style.background='transparent'\">"
                    f'{_escape_html(str(display))} <span style="color:var(--muted);font-size:0.75rem;">#{pk_safe}</span></div>'
                )
            return Response.html("".join(html_parts))

        return autocomplete_handler

    # ── Confirm Delete Dialog (HTMX) ─────────────────────────────────────

    def _make_confirm_delete_dialog(self, config):
        """Return delete confirmation dialog HTML for HTMX modal."""

        async def confirm_delete(request):
            db = self._get_db()
            meta = config.model_class._meta
            pk = request.path_params.get("id")

            row = await self._get_row(config, pk, request=request)
            instance_str = str(pk)
            if row:
                row_dict = (
                    dict(zip(meta.column_names, row))
                    if not isinstance(row, dict)
                    else row
                )
                # Use first string field as repr
                for f in config.fields:
                    if f.python_type is str and f.name in row_dict:
                        instance_str = str(row_dict[f.name])
                        break

            ctx = self._base_context()
            ctx.update(
                {
                    "model_name": config.name,
                    "slug": config.slug,
                    "pk": pk,
                    "instance_str": instance_str,
                }
            )
            html = self.engine.render_string(TEMPLATE_DELETE_DIALOG, ctx)
            return Response.html(html)

        return confirm_delete

    # ── Inline Helpers ──────────────────────────────────────────────────

    # ── History View ──────────────────────────────────────────────────────

    def _make_history_view(self, config):
        """Object history view — shows audit log for a specific object."""

        async def history_view(request):
            denied = await self._require_view_or_403(config, request)
            if denied is not None:
                return denied
            db = self._get_db()
            meta = config.model_class._meta
            pk = _parse_pk(request.path_params.get("id"))
            if pk is None:
                return Response.html("Not found", status=404)

            # Get object repr
            row = await self._get_row(config, pk, request=request)
            object_repr = str(pk)
            if row:
                row_dict = (
                    dict(zip(meta.column_names, row))
                    if not isinstance(row, dict)
                    else row
                )
                for f in config.fields:
                    if f.python_type is str and f.name in row_dict:
                        object_repr = str(row_dict[f.name])
                        break

            # Get audit log entries (table may not exist yet)
            entries = []
            try:
                audit = AuditLog(db)
                entries = await audit.get_object_history(config.slug, str(pk))
            # blind-except: audit history is optional; if the audit table is absent the history view renders with no entries rather than 500ing.
            except Exception:
                pass

            ctx = self._base_context()
            ctx.update(
                {
                    "title": f"History: {object_repr}",
                    "model_name": config.name,
                    "slug": config.slug,
                    "pk": pk,
                    "object_repr": object_repr,
                    "entries": entries,
                }
            )
            html = self._render(TEMPLATE_HISTORY, ctx, config)
            return Response.html(html)

        return history_view

    # ── Audit Logging ────────────────────────────────────────────────────

    async def _audit_log(
        self, action, config, object_id, object_repr="", changes=None, request=None
    ):
        """Log an admin action to the audit trail."""
        # Native telemetry — bumped before the audit DB write so the
        # metric is recorded even if the audit table is unavailable.
        # Only the canonical actions are tracked; anything else falls
        # under "other" so we don't proliferate label values.
        if action in ("add", "change", "delete"):
            _admin_actions_total.inc_tuple((config.slug, action))
        try:
            db = self._get_db()
            audit = AuditLog(db)
            user = request._admin_user if request else None
            user_id = user.get("user_id") if user else None
            username = user.get("username", "") if user else ""

            if action == "add":
                await audit.log_add(
                    user_id, config.slug, str(object_id), object_repr, username
                )
            elif action == "change":
                await audit.log_change(
                    user_id, config.slug, str(object_id), object_repr, changes, username
                )
            elif action == "delete":
                await audit.log_delete(
                    user_id, config.slug, str(object_id), object_repr, username
                )
        # blind-except: audit logging is a side-channel; a write failure must never roll back or break the admin action it is recording.
        except Exception:
            pass  # Audit logging should never break the main operation

    # ── Inline Helpers ──────────────────────────────────────────────────

    async def _build_inline_context(self, config, parent_pk=None):
        """Build template context for all inlines on a form."""
        inlines_ctx = []
        db = self._get_db()
        for inline in config.inlines:
            inline_fields = _get_inline_fields(inline)
            inline_meta = inline.model_class._meta
            prefix = f"inline_{inline.model_class.__name__.lower()}"
            slug = inline.model_class.__name__.lower()

            # Batch FK display resolution — 1 query per FK table instead of N+1
            fk_display_cache = {}  # {table: {pk_int: "name #pk"}}

            # Load existing rows for edit view
            existing_rows = []
            if parent_pk is not None and inline.fk_field:
                order_clause = (
                    f"ORDER BY {inline.ordering}"
                    if inline.ordering
                    else f"ORDER BY {inline_meta.pk_field}"
                )
                inl_params: list[object] = [int(parent_pk)]
                inl_tsuf = tenant_where_suffix(inline.model_class, inl_params)
                rows = await db.query(
                    f"SELECT * FROM {inline_meta.table} WHERE {inline.fk_field} = $1{inl_tsuf} {order_clause}",
                    *inl_params,
                )
                all_cols = inline_meta.column_names

                # Collect all FK values per table, then batch-load display names
                fk_values_by_table = {}  # {table: set of pk ints}
                for row in rows:
                    row_dict = (
                        dict(zip(all_cols, row)) if not isinstance(row, dict) else row
                    )
                    for af in inline_fields:
                        if af.foreign_key:
                            val = row_dict.get(af.name)
                            if val:
                                fk_values_by_table.setdefault(
                                    af.foreign_key, set()
                                ).add(int(val))

                # One batch query per FK table
                for fk_table, pk_set in fk_values_by_table.items():
                    if not pk_set:
                        continue
                    display_col = "id"
                    for col_name in _LABEL_CANDIDATES:
                        try:
                            test = await db.query_one(
                                f"SELECT {col_name} FROM {fk_table} LIMIT 1"
                            )
                            if test is not None:
                                display_col = col_name
                                break
                        # blind-except: probing which label column exists on the FK target; a failed candidate SELECT just tries the next one and falls back to "id", so it must not propagate.
                        except Exception:
                            continue
                    placeholders = ", ".join(f"${i + 1}" for i in range(len(pk_set)))
                    fk_rows = await db.query(
                        f"SELECT id, {display_col} FROM {fk_table} WHERE id IN ({placeholders})",
                        *list(pk_set),
                    )
                    cache = {}
                    for fr in fk_rows:
                        pk = fr["id"] if isinstance(fr, dict) else fr[0]
                        name = fr[display_col] if isinstance(fr, dict) else fr[1]
                        cache[pk] = f"{name} #{pk}"
                    fk_display_cache[fk_table] = cache

                for idx, row in enumerate(rows):
                    row_dict = (
                        dict(zip(all_cols, row)) if not isinstance(row, dict) else row
                    )
                    row_fields = []
                    for af in inline_fields:
                        val = row_dict.get(af.name, af.default)
                        if val is None:
                            val = ""
                        display_value = str(val)
                        if af.foreign_key and val:
                            display_value = fk_display_cache.get(
                                af.foreign_key, {}
                            ).get(int(val), str(val))
                        row_fields.append(
                            {
                                "name": af.name,
                                "widget": af.widget,
                                "value": val,
                                "display_value": display_value,
                                "foreign_key": af.foreign_key,
                                "attrs": af.attrs,
                                "choices": af.choices or [],
                            }
                        )
                    existing_rows.append(
                        {
                            "index": idx,
                            "pk": row_dict.get(inline_meta.pk_field),
                            "fields": row_fields,
                        }
                    )

            # Empty rows for new entries
            empty_rows = []
            for idx in range(inline.extra):
                row_fields = []
                for af in inline_fields:
                    val = af.default if af.default is not None else ""
                    row_fields.append(
                        {
                            "name": af.name,
                            "widget": af.widget,
                            "value": val,
                            "display_value": "",
                            "foreign_key": af.foreign_key,
                            "attrs": af.attrs,
                            "choices": af.choices or [],
                        }
                    )
                empty_rows.append({"index": idx, "fields": row_fields})

            columns = [{"name": f.name, "label": f.label} for f in inline_fields]

            inlines_ctx.append(
                {
                    "slug": slug,
                    "parent_slug": config.slug,
                    "name": inline.model_class.__name__,
                    "prefix": prefix,
                    "columns": columns,
                    "rows": existing_rows,
                    "empty_rows": empty_rows,
                    "can_delete": inline.can_delete,
                    "total": len(existing_rows) + inline.extra,
                    "initial": len(existing_rows),
                    "next_index": len(existing_rows) + inline.extra,
                }
            )

        return inlines_ctx

    async def _save_inlines(self, config, parent_pk, form_data):
        """Process inline form data: create new, update existing, delete marked."""
        db = self._get_db()
        for inline in config.inlines:
            inline_fields = _get_inline_fields(inline)
            inline_meta = inline.model_class._meta
            prefix = f"inline_{inline.model_class.__name__.lower()}"

            # Process existing rows (update or delete)
            idx = 0
            while True:
                pk_key = f"{prefix}-{idx}-id"
                if pk_key not in form_data:
                    break
                row_pk = form_data.get(pk_key)

                # Check DELETE checkbox
                if form_data.get(f"{prefix}-{idx}-DELETE"):
                    await self._delete_row(inline.model_class, row_pk)
                else:
                    # Update
                    update_cols = []
                    update_vals = []
                    for af in inline_fields:
                        raw = form_data.get(f"{prefix}-{idx}-{af.name}")
                        if raw is not None:
                            try:
                                val = _coerce_value(raw, af.python_type)
                            except ValueError, TypeError:
                                val = raw
                            update_cols.append(af.name)
                            update_vals.append(val)
                    if update_cols:
                        row_values = dict(zip(update_cols, update_vals))
                        row_values[inline_meta.pk_field] = int(row_pk)
                        await self._update_row(inline.model_class, row_pk, row_values)
                idx += 1

            # Process new rows
            new_idx = 0
            while True:
                first_field = inline_fields[0].name if inline_fields else None
                if first_field is None:
                    break
                check_key = f"{prefix}-new-{new_idx}-{first_field}"
                if check_key not in form_data:
                    break

                # Check if row has any data
                has_data = False
                insert_cols = [inline.fk_field] if inline.fk_field else []
                insert_vals = [int(parent_pk)] if inline.fk_field else []
                for af in inline_fields:
                    raw = form_data.get(f"{prefix}-new-{new_idx}-{af.name}", "")
                    if raw:
                        has_data = True
                    try:
                        val = _coerce_value(raw, af.python_type) if raw else af.default
                    except ValueError, TypeError:
                        val = raw or af.default
                    insert_cols.append(af.name)
                    insert_vals.append(val)

                if has_data:
                    col_str = ", ".join(insert_cols)
                    placeholders = ", ".join(
                        f"${i + 1}" for i in range(len(insert_cols))
                    )
                    await db.execute(
                        f"INSERT INTO {inline_meta.table} ({col_str}) VALUES ({placeholders})",
                        *insert_vals,
                    )
                new_idx += 1

    def _prepopulated_context(self, config) -> dict[str, bool | str]:
        """Build prepopulated_fields context for form templates."""
        pp = config.prepopulated_fields
        return {
            "prepopulated_fields": bool(pp),
            "prepopulated_fields_json": fast_json_dumps(pp).decode() if pp else "{}",
        }

    def _render_inline_html(self, config, inlines_ctx):
        """Pre-render the inline section HTML."""
        if not inlines_ctx:
            return ""
        ctx = self._base_context()
        ctx["inlines"] = inlines_ctx
        return self.engine.render_string(TEMPLATE_INLINE_SECTION, ctx)

    def _make_inline_row_handler(self, config):
        """HTMX endpoint: return a single new inline form row."""

        async def inline_row_handler(request):
            inline_slug = request.GET.get("inline", "")
            index = int(request.GET.get("index", "0"))

            inline = next(
                (
                    i
                    for i in config.inlines
                    if i.model_class.__name__.lower() == inline_slug
                ),
                None,
            )
            if inline is None:
                return Response.html("")

            inline_fields = _get_inline_fields(inline)
            prefix = f"inline_{inline_slug}"

            fields = []
            for af in inline_fields:
                val = af.default if af.default is not None else ""
                fields.append(
                    {
                        "name": af.name,
                        "widget": af.widget,
                        "value": val,
                        "attrs": af.attrs,
                        "choices": af.choices or [],
                    }
                )

            ctx = {
                "inline_slug": inline_slug,
                "prefix_name": prefix,
                "index": index,
                "fields": fields,
                "can_delete": inline.can_delete,
            }
            html = self.engine.render_string(TEMPLATE_INLINE_ROW, ctx)
            return Response.html(html)

        return inline_row_handler

    # ── Add View ──────────────────────────────────────────────────────────

    def _make_add_view(self, config):
        async def add_view(request):
            perms = self._get_model_perms(config, request)
            if not perms["can_add"]:
                return Response.html(
                    "<h2>Permission denied</h2><p>You do not have permission to add this object.</p>",
                    status=403,
                )
            fk_cache, fk_ch = await self._resolve_fk_display_values(config, {})
            field_groups = self._build_form_field_groups(
                config,
                values={},
                request=request,
                obj=None,
                fk_display_cache=fk_cache,
                fk_choices=fk_ch,
            )
            inlines_ctx = await self._build_inline_context(config, parent_pk=None)
            html = self._render(
                self._resolve_template(config, "form", TEMPLATE_FORM),
                {
                    "title": f"Add {config.name}",
                    "model_name": config.name,
                    "slug": config.slug,
                    "field_groups": field_groups,
                    "inline_html": self._render_inline_html(config, inlines_ctx),
                    "is_edit": False,
                    "pk": None,
                    "error": "",
                    "view_only": False,
                    "view_on_site_url": None,
                    "save_as": False,
                    "save_on_top": config.save_on_top,
                    "m2m_fields": await self._load_m2m_data(config, None),
                    "perms": perms,
                    **self._prepopulated_context(config),
                },
                config,
            )
            return Response.html(html)

        return add_view

    def _make_add_handler(self, config):
        async def add_handler(request):
            error = await self._enforce_post_security(config, request, "can_add")
            if error:
                return error

            db = self._get_db()
            meta = config.model_class._meta

            form_data = await request.form()
            values, error = self._parse_form_data(config, form_data)

            if error:
                fk_cache, fk_ch = await self._resolve_fk_display_values(
                    config, form_data
                )
                field_groups = self._build_form_field_groups(
                    config,
                    values=form_data,
                    request=request,
                    obj=None,
                    fk_display_cache=fk_cache,
                    fk_choices=fk_ch,
                )
                inlines_ctx = await self._build_inline_context(config, parent_pk=None)
                html = self._render(
                    self._resolve_template(config, "form", TEMPLATE_FORM),
                    {
                        "title": f"Add {config.name}",
                        "model_name": config.name,
                        "slug": config.slug,
                        "field_groups": field_groups,
                        "inline_html": self._render_inline_html(config, inlines_ctx),
                        "is_edit": False,
                        "pk": None,
                        "error": error,
                    },
                    config,
                )
                return Response.html(html, status=400)

            # Run save hooks (new-style: request, values, is_edit, obj; old-style: values, is_edit)
            for hook in config.save_hooks:
                if _hook_param_count(hook) >= 4:
                    values = await hook(request, values, False, None)
                else:
                    values = await hook(values, False)

            # Atomicity: the parent INSERT, inline saves, M2M saves and the
            # audit-log write must all commit together or not at all. Wrapping
            # the whole save in one transaction keeps it all-or-nothing, so a
            # failure after the parent INSERT (e.g. a bad inline row) can never
            # leave an orphaned committed parent with no children. (Nested
            # db.transaction() calls inside the helpers become savepoints under
            # this outer BEGIN/COMMIT.)
            async with db.transaction():
                # INSERT
                columns = [
                    k for k in values if k != meta.pk_field or not meta.auto_field
                ]
                col_str = ", ".join(columns)
                placeholders = ", ".join(f"${i + 1}" for i in range(len(columns)))
                vals = [values[c] for c in columns]

                sql = f"INSERT INTO {meta.table} ({col_str}) VALUES ({placeholders})"
                if meta.auto_field:
                    sql += f" RETURNING {meta.auto_field}"
                    new_id = await db.query_val(sql, *vals)
                else:
                    await db.execute(sql, *vals)
                    new_id = values.get(meta.pk_field, "")

                # Save inline data
                if config.inlines and new_id:
                    await self._save_inlines(config, new_id, form_data)

                # Save M2M data
                if config.filter_horizontal and new_id:
                    await self._save_m2m_data(config, new_id, form_data)

                # Audit log
                obj_repr = values.get(
                    "name", values.get("title", values.get("username", str(new_id)))
                )
                await self._audit_log(
                    "add", config, new_id, str(obj_repr), request=request
                )

                # Post-save hook
                if config.on_add is not None:
                    await config.on_add(request, values)

            return self._post_save_redirect(
                config, request, new_id, "add", f"{config.name}+created+successfully"
            )

        return add_handler

    # ── Edit View ─────────────────────────────────────────────────────────

    def _make_edit_view(self, config):
        async def edit_view(request):
            denied = await self._require_view_or_403(config, request)
            if denied is not None:
                return denied
            db = self._get_db()
            meta = config.model_class._meta
            pk = request.path_params.get("id")

            # Check view-only mode: can view but not change
            perms = self._get_model_perms(config, request)
            view_only = perms.get("can_view", True) and not perms.get(
                "can_change", True
            )

            row = await self._get_row(config, pk, request=request)
            if not row:
                return Response.html("<h1>Not Found</h1>", status=404)

            row_dict = (
                dict(zip(meta.column_names, row)) if not isinstance(row, dict) else row
            )

            # Pre-populate virtual _rc_* fields from rule_config JSON on edit
            if config.slug == "permission-rules" and "rule_config" in row_dict:
                try:
                    rc = (
                        fast_json_loads(row_dict["rule_config"])
                        if isinstance(row_dict["rule_config"], str)
                        else row_dict["rule_config"]
                    )
                    rt = row_dict.get("rule_type", "")
                    if rt == "is_owner":
                        row_dict["_rc_owner_field"] = rc.get("owner_field", "user_id")
                    elif rt == "time_window":
                        row_dict["_rc_start"] = rc.get("start", "09:00")
                        row_dict["_rc_end"] = rc.get("end", "17:00")
                        row_dict["_rc_timezone"] = rc.get("timezone", "UTC")
                    elif rt == "ip_range":
                        row_dict["_rc_ranges"] = "\n".join(rc.get("ranges", []))
                    elif rt == "field_match":
                        row_dict["_rc_field"] = rc.get("field", "")
                        row_dict["_rc_values"] = "\n".join(rc.get("values", []))
                    elif rt == "custom":
                        row_dict["_rc_module"] = rc.get("module", "")
                        row_dict["_rc_function"] = rc.get("function", "")
                except ValueError, TypeError, AttributeError:
                    pass

            fk_cache, fk_ch = await self._resolve_fk_display_values(config, row_dict)
            field_groups = self._build_form_field_groups(
                config,
                values=row_dict,
                request=request,
                obj=row_dict,
                fk_display_cache=fk_cache,
                fk_choices=fk_ch,
            )
            inlines_ctx = await self._build_inline_context(config, parent_pk=pk)

            # Extra links for specific models (e.g., effective perms for users)
            extra_links = []
            if config.slug == "users":
                extra_links.append(
                    {
                        "url": f"{self.prefix}/users/{pk}/effective-permissions/",
                        "label": "View Effective Permissions",
                    }
                )

            html = self._render(
                self._resolve_template(config, "form", TEMPLATE_FORM),
                {
                    "title": f"Edit {config.name}",
                    "model_name": config.name,
                    "slug": config.slug,
                    "field_groups": field_groups,
                    "inline_html": self._render_inline_html(config, inlines_ctx),
                    "is_edit": True,
                    "pk": pk,
                    "error": "",
                    "view_only": view_only,
                    "view_on_site_url": config.view_on_site(row_dict)
                    if config.view_on_site
                    else None,
                    "save_as": config.save_as,
                    "save_on_top": config.save_on_top,
                    "extra_links": extra_links,
                    "m2m_fields": await self._load_m2m_data(config, pk),
                    **self._prepopulated_context(config),
                },
                config,
            )
            return Response.html(html)

        return edit_view

    def _make_edit_handler(self, config):
        async def edit_handler(request):
            error = await self._enforce_post_security(config, request, "can_change")
            if error:
                return error

            db = self._get_db()
            meta = config.model_class._meta
            pk = request.path_params.get("id")

            form_data = await request.form()

            values, error = self._parse_form_data(config, form_data)

            if error:
                fk_cache, fk_ch = await self._resolve_fk_display_values(
                    config, form_data
                )
                field_groups = self._build_form_field_groups(
                    config,
                    values=form_data,
                    request=request,
                    obj=form_data,
                    fk_display_cache=fk_cache,
                    fk_choices=fk_ch,
                )
                inlines_ctx = await self._build_inline_context(config, parent_pk=pk)
                html = self._render(
                    self._resolve_template(config, "form", TEMPLATE_FORM),
                    {
                        "title": f"Edit {config.name}",
                        "model_name": config.name,
                        "slug": config.slug,
                        "field_groups": field_groups,
                        "inline_html": self._render_inline_html(config, inlines_ctx),
                        "is_edit": True,
                        "pk": pk,
                        "error": error,
                    },
                    config,
                )
                return Response.html(html, status=400)

            # Run save hooks (new-style: request, values, is_edit, obj; old-style: values, is_edit)
            existing_obj = await self._get_row(config, pk, request=request)
            for hook in config.save_hooks:
                if _hook_param_count(hook) >= 4:
                    values = await hook(request, values, True, existing_obj)
                else:
                    values = await hook(values, True)

            # save_as: INSERT as new object instead of UPDATE. Same atomicity
            # requirement as the add handler — parent INSERT + audit must be
            # all-or-nothing so a failure can't leave an orphaned committed row.
            if config.save_as and form_data.get("_save_as"):
                async with db.transaction():
                    insert_cols = [
                        k for k in values if k != meta.pk_field or not meta.auto_field
                    ]
                    col_str = ", ".join(insert_cols)
                    placeholders = ", ".join(
                        f"${i + 1}" for i in range(len(insert_cols))
                    )
                    vals = [values[c] for c in insert_cols]
                    sql = (
                        f"INSERT INTO {meta.table} ({col_str}) VALUES ({placeholders})"
                    )
                    if meta.auto_field:
                        sql += f" RETURNING {meta.auto_field}"
                        new_id = await db.query_val(sql, *vals)
                    else:
                        await db.execute(sql, *vals)
                        new_id = values.get(meta.pk_field, "")
                    obj_repr = values.get("name", values.get("title", str(new_id)))
                    await self._audit_log(
                        "add", config, new_id, str(obj_repr), request=request
                    )
                    if config.on_add is not None:
                        await config.on_add(request, values)
                return self._post_save_redirect(
                    config,
                    request,
                    new_id,
                    "add",
                    f"{config.name}+duplicated+successfully",
                )

            # Fetch old row for audit diff BEFORE updating. This is scoped by
            # tenant + get_queryset, so None means the PK is out of this admin's
            # scope (or gone) — refuse rather than UPDATE an out-of-scope row.
            old_row = await self._get_row(config, pk, request=request)
            if old_row is None:
                return Response.html("Not found", status=404)

            # Atomicity: the parent UPDATE, inline saves, M2M saves and the
            # audit-log write must all commit together or not at all, so a
            # failure after the UPDATE can't leave partially-applied children.
            async with db.transaction():
                # UPDATE (tenant-scoped + get_queryset-scoped via _update_row)
                await self._update_row(config, pk, values, request=request)

                # Save inline data
                if config.inlines:
                    await self._save_inlines(config, pk, form_data)

                # Save M2M data
                if config.filter_horizontal:
                    await self._save_m2m_data(config, pk, form_data)

                # Compute old-vs-new diff for audit log. update_cols is the set
                # of columns actually written by _update_row (except the PK).
                changes_diff = {}
                update_cols = [k for k in values if k != meta.pk_field]
                for col in update_cols:
                    old_val = old_row.get(col) if old_row else None
                    new_val = values.get(col)
                    if str(old_val) != str(new_val):
                        changes_diff[col] = {"old": old_val, "new": new_val}

                obj_repr = values.get(
                    "name", values.get("title", values.get("username", str(pk)))
                )
                await self._audit_log(
                    "change",
                    config,
                    pk,
                    str(obj_repr),
                    changes=changes_diff,
                    request=request,
                )

                # Post-save hook
                if config.on_change is not None:
                    await config.on_change(request, values)

            return self._post_save_redirect(
                config, request, pk, "change", f"{config.name}+updated+successfully"
            )

        return edit_handler

    # ── Delete Handler ────────────────────────────────────────────────────

    def _make_delete_handler(self, config):
        async def delete_handler(request):
            error = await self._enforce_post_security(config, request, "can_delete")
            if error:
                return error

            db = self._get_db()
            meta = config.model_class._meta
            pk = _parse_pk(request.path_params.get("id"))
            if pk is None:
                return Response.html("Not found", status=404)

            # Run delete hooks (new-style: request, pk, obj; old-style: pk).
            # The fetch is scoped by tenant + get_queryset, so None means the PK
            # is out of this admin's scope (or already gone) — 404 rather than
            # issue a DELETE that could reach an out-of-scope row.
            delete_obj = await self._get_row(config, int(pk), request=request)
            if delete_obj is None:
                return Response.html("Not found", status=404)
            for hook in config.delete_hooks:
                if _hook_param_count(hook) >= 3:
                    await hook(request, int(pk), delete_obj)
                else:
                    await hook(int(pk))

            await self._delete_row(config, pk, request=request)

            # Audit log
            await self._audit_log("delete", config, pk, str(pk), request=request)

            # Post-delete hook
            if config.on_delete is not None:
                await config.on_delete(request, int(pk))

            return self._post_save_redirect(
                config, request, pk, "delete", f"{config.name}+deleted+successfully"
            )

        return delete_handler

    # ── Helpers ───────────────────────────────────────────────────────────

    def _get_db(self):
        """Get the database connection from the app."""
        db = self.app._db
        if db is None:
            db = _get_db_fallback()
        return db

    # ── Tenant-aware query helpers ────────────────────────────────────────
    # All single-record admin queries go through these three methods.
    # Tenant filtering is applied automatically when the model uses TenantMixin.

    async def _query_scoped(
        self, model_class, sql_template: str, params: list[object]
    ) -> list:
        """Execute a SELECT with tenant filtering. sql_template should have a {tenant} placeholder.

        For admin queries that need tenant + other WHERE conditions.
        """
        db = self._get_db()
        conds: list[str] = []
        t_params: list[object] = []
        inject_tenant_condition(model_class, conds, t_params)
        tenant_clause = f" AND {conds[0]}" if conds else ""
        all_params = list(params) + t_params
        return await db.query(
            sql_template.replace("{tenant}", tenant_clause), *all_params
        )

    async def _get_row(self, config_or_model, pk, request=None) -> dict | None:
        """SELECT a single row by PK, scoped to current tenant + get_queryset.

        config_or_model: ModelConfig or Model class directly (for inlines).
        request: if provided and config has get_queryset, applies dynamic filtering.
        """
        db = self._get_db()
        config = config_or_model if isinstance(config_or_model, ModelConfig) else None
        model_class = config.model_class if config else config_or_model
        meta = model_class._meta
        params: list[object] = [int(pk)]
        tsuf = tenant_where_suffix(model_class, params)
        # Apply get_queryset filter to single-row fetch (prevent accessing out-of-scope rows)
        if (
            config is not None
            and config.get_queryset is not None
            and request is not None
        ):
            qs_filter = await config.get_queryset(request)
            if qs_filter is not None:
                for col, val in qs_filter.items():
                    if col in meta.column_names:
                        params.append(val)
                        tsuf += f" AND {col} = ${len(params)}"
        return await db.query_one(
            f"SELECT * FROM {meta.table} WHERE {meta.pk_field} = $1{tsuf}", *params
        )

    async def _get_queryset_suffix(self, config_or_model, meta, request, params) -> str:
        """Build the get_queryset WHERE suffix, appending bind params in place.

        Mirrors the row-scoping applied by _get_row so mutations (UPDATE/DELETE)
        can't touch PKs outside an admin's get_queryset scope. Returns "" when
        no config/get_queryset/request is available (e.g. inline model classes).
        """
        config = config_or_model if isinstance(config_or_model, ModelConfig) else None
        if config is None or config.get_queryset is None or request is None:
            return ""
        qs_filter = await config.get_queryset(request)
        if qs_filter is None:
            return ""
        suffix = ""
        for col, val in qs_filter.items():
            if col in meta.column_names:
                params.append(val)
                suffix += f" AND {col} = ${len(params)}"
        return suffix

    async def _delete_row(self, config_or_model, pk, request=None) -> None:
        """DELETE a single row by PK, scoped to current tenant + get_queryset."""
        db = self._get_db()
        model_class = (
            config_or_model.model_class
            if isinstance(config_or_model, ModelConfig)
            else config_or_model
        )
        meta = model_class._meta
        params: list[object] = [int(pk)]
        tsuf = tenant_where_suffix(model_class, params)
        tsuf += await self._get_queryset_suffix(config_or_model, meta, request, params)
        await db.execute(
            f"DELETE FROM {meta.table} WHERE {meta.pk_field} = $1{tsuf}", *params
        )

    async def _update_row(
        self, config_or_model, pk, values: dict[str, object], request=None
    ) -> None:
        """UPDATE a single row by PK, scoped to current tenant + get_queryset."""
        db = self._get_db()
        model_class = (
            config_or_model.model_class
            if isinstance(config_or_model, ModelConfig)
            else config_or_model
        )
        meta = model_class._meta
        update_cols = [k for k in values if k != meta.pk_field]
        set_clause = ", ".join(f"{c} = ${i + 1}" for i, c in enumerate(update_cols))
        vals: list[object] = [values[c] for c in update_cols]
        vals.append(int(pk))
        pk_idx = len(vals)
        tsuf = tenant_where_suffix(model_class, vals)
        tsuf += await self._get_queryset_suffix(config_or_model, meta, request, vals)
        await db.execute(
            f"UPDATE {meta.table} SET {set_clause} WHERE {meta.pk_field} = ${pk_idx}{tsuf}",
            *vals,
        )

    async def _resolve_fk_display_values(self, config, values):
        """Batch-resolve FK fields: display strings + select options.

        Returns (fk_display_cache, fk_choices) where:
        - fk_display_cache: {fk_table: {pk: "name #pk"}} for current values
        - fk_choices: {fk_table: [(pk, "name #pk"), ...]} for <select> options
        """
        fk_tables: set[str] = set()
        for af in config.form_fields:
            if af.foreign_key:
                fk_tables.add(af.foreign_key)

        fk_display_cache: dict[str, dict[int, str]] = {}
        fk_choices: dict[str, list[tuple[int, str]]] = {}
        if not fk_tables:
            return fk_display_cache, fk_choices

        db = self._get_db()
        display_col_cache = config._fk_display_col_cache
        now = _time.monotonic()
        for fk_table in fk_tables:
            # Short-TTL cache: the LIMIT 201 scan + option build is identical
            # across renders, so serve a recent copy and skip the query. Copy the
            # cached dict before augmenting with per-request form values below.
            cached = self._fk_display_values_cache.get(fk_table)
            if cached is not None and cached[0] > now:
                fk_display_cache[fk_table] = dict(cached[1])
                fk_choices[fk_table] = cached[2]
                continue

            # Resolve display column (cached across requests)
            display_col = display_col_cache.get(fk_table)
            if display_col is None:
                display_col = "id"
                safe_table = _assert_safe_ident(fk_table)
                for candidate in _LABEL_CANDIDATES:
                    try:
                        test = await db.query_one(
                            f"SELECT {candidate} FROM {safe_table} WHERE {candidate} IS NOT NULL AND {candidate} != '' LIMIT 1"
                        )
                        if test is not None:
                            display_col = candidate
                            break
                    # blind-except: probing which label column exists on the FK target; a failed candidate SELECT just tries the next one and falls back to "id", so it must not propagate.
                    except Exception:
                        continue
                display_col_cache[fk_table] = display_col

            # Load rows for <select> dropdown (up to 201 to detect overflow)
            safe_table = _assert_safe_ident(fk_table)
            safe_col = _assert_safe_ident(display_col)
            fk_rows = await db.query(
                f"SELECT id, {safe_col} FROM {safe_table} ORDER BY {safe_col} LIMIT 201"
            )
            cache: dict[int, str] = {}
            choices: list[tuple[str, str]] = []
            truncated = len(fk_rows) > 200
            for fr in fk_rows[:200]:
                pk = fr["id"] if isinstance(fr, dict) else fr[0]
                name = fr[display_col] if isinstance(fr, dict) else fr[1]
                label = f"{name} #{pk}"
                cache[pk] = label
                choices.append((str(pk), label))
            if truncated:
                # Too many rows for a <select> — use autocomplete search instead
                choices = []
            self._fk_display_values_cache[fk_table] = (
                now + _FK_DISPLAY_VALUES_TTL,
                cache,
                choices,
            )
            # Hand out a copy so the per-request augmentation below never
            # pollutes the shared cached dict.
            fk_display_cache[fk_table] = dict(cache)
            fk_choices[fk_table] = choices

        # Ensure current form values are in the display cache even if they
        # weren't in the first 200 rows (e.g., editing a record that references
        # row #500 in a large FK table).
        for af in config.form_fields:
            if not af.foreign_key:
                continue
            val = values.get(af.name, af.default)
            if not val or val == "":
                continue
            try:
                pk_int = int(val)
            except ValueError, TypeError:
                continue
            table_cache = fk_display_cache.get(af.foreign_key, {})
            if pk_int not in table_cache:
                safe_fk = _assert_safe_ident(af.foreign_key)
                dc = display_col_cache.get(af.foreign_key, "id")
                safe_dc = _assert_safe_ident(dc)
                row = await db.query_one(
                    f"SELECT id, {safe_dc} FROM {safe_fk} WHERE id = $1",
                    pk_int,
                )
                if row:
                    rpk = row["id"] if isinstance(row, dict) else row[0]
                    # BUG FIX: index dict rows by THIS table's display column
                    # (dc), not the loop-leftover `display_col` from the FK-table
                    # loop above — that produced KeyError / wrong labels when FK
                    # tables had different display columns.
                    rname = row[dc] if isinstance(row, dict) else row[1]
                    table_cache[rpk] = f"{rname} #{rpk}"

        return fk_display_cache, fk_choices

    def _build_form_fields(
        self, config, values, fk_display_cache=None, fk_choices=None
    ):
        """Build form field dicts for template rendering.

        Applies formfield_overrides from config to override widget types and attrs.
        Pass fk_display_cache/fk_choices from _resolve_fk_display_values() to
        render FK fields as <select> dropdowns with human-readable labels.
        """
        if fk_display_cache is None:
            fk_display_cache = {}
        if fk_choices is None:
            fk_choices = {}
        overrides = config.formfield_overrides
        form_fields = []
        for af in config.form_fields:
            val = values.get(af.name, af.default)
            if val is None:
                val = ""

            # Apply formfield_overrides: match by Python type
            widget = af.widget
            attrs = dict(af.attrs)
            if af.python_type in overrides:
                override = overrides[af.python_type]
                if "widget" in override:
                    widget = override["widget"]
                if "attrs" in override:
                    attrs.update(override["attrs"])

            if widget == "checkbox":
                val = bool(val) if val != "" else False

            help_text = ""
            if af.foreign_key:
                help_text = f"Foreign key → {af.foreign_key}"
            if attrs.get("maxlength"):
                help_text += f" (max {attrs['maxlength']} chars)"

            # For FK fields, resolve display value + choices from batch cache
            display_value = ""
            fk_field_choices = []
            if af.foreign_key:
                if val and val != "":
                    display_value = fk_display_cache.get(af.foreign_key, {}).get(
                        int(val), str(val)
                    )
                # Use <select> dropdown with preloaded choices
                fk_field_choices = fk_choices.get(af.foreign_key, [])
                if fk_field_choices:
                    widget = "select"

            # raw_id_fields: suppress FK select, show plain number input
            is_raw_id = af.name in config.raw_id_fields
            if is_raw_id and af.foreign_key:
                widget = "number"
                fk_field_choices = []
            # radio_fields: render select/choice as radio buttons
            radio_layout = config.radio_fields.get(af.name)

            form_fields.append(
                {
                    "name": af.name,
                    "label": af.label,
                    "widget": widget,
                    "value": str(val) if af.foreign_key and val else val,
                    "required": af.required,
                    "is_readonly": af.is_readonly,
                    "attrs": attrs,
                    "choices": fk_field_choices or (af.choices or []),
                    "help": help_text,
                    "foreign_key": af.foreign_key if not is_raw_id else None,
                    "display_value": display_value,
                    "radio_layout": radio_layout,
                }
            )
        return form_fields

    def _build_form_field_groups(
        self,
        config,
        values,
        request=None,
        obj=None,
        fk_display_cache=None,
        fk_choices=None,
    ):
        """Build form field groups organized by fieldsets for template rendering.

        request/obj: when provided, enables dynamic hooks:
        - get_readonly_fields(request, obj) → extra readonly fields
        - get_fieldsets(request, obj) → override static fieldsets

        fk_display_cache/fk_choices: pre-resolved FK display values and
        select options from _resolve_fk_display_values().
        """
        all_fields = self._build_form_fields(
            config, values, fk_display_cache=fk_display_cache, fk_choices=fk_choices
        )
        field_map = {f["name"]: f for f in all_fields}

        # Compute effective readonly set: static + dynamic
        readonly_set = set(config.readonly_fields)
        if config.get_readonly_fields is not None and request is not None:
            dynamic_readonly = config.get_readonly_fields(request, obj)
            if dynamic_readonly:
                readonly_set.update(dynamic_readonly)

        # Use dynamic fieldsets if available, else static
        fieldsets = config.fieldsets
        if config.get_fieldsets is not None and request is not None:
            dynamic_fs = config.get_fieldsets(request, obj)
            if dynamic_fs is not None:
                fieldsets = dynamic_fs

        if not fieldsets:
            # Apply readonly to ungrouped fields
            for f in all_fields:
                if f["name"] in readonly_set:
                    f["is_readonly"] = True
            return [
                {"title": None, "fields": all_fields, "classes": [], "description": ""}
            ]

        groups = []
        used = set()
        for fs in fieldsets:
            group_fields = []
            for fname in fs.fields:
                if fname in field_map:
                    fdict = field_map[fname]
                    if fname in readonly_set:
                        fdict = dict(fdict, is_readonly=True)
                    group_fields.append(fdict)
                    used.add(fname)
            groups.append(
                {
                    "title": fs.title,
                    "fields": group_fields,
                    "classes": fs.classes,
                    "description": fs.description,
                }
            )

        # Include any fields not covered by fieldsets
        remaining = [f for f in all_fields if f["name"] not in used]
        if remaining:
            groups.append(
                {
                    "title": "Other",
                    "fields": remaining,
                    "classes": [],
                    "description": "",
                }
            )

        return groups

    def _parse_form_data(self, config, form_data):
        """Parse and validate form data. Returns (values_dict, error_string)."""
        values = {}
        for af in config.form_fields:
            if af.is_auto:
                continue

            raw = form_data.get(af.name)

            # Checkbox: missing means False
            if af.widget == "checkbox":
                values[af.name] = raw is not None and raw != ""
                continue

            # Required check
            if af.required and (raw is None or raw == ""):
                return {}, f"{af.label} is required"

            # Skip empty non-required
            if raw is None or raw == "":
                if af.default is not None and af.default is not _MISSING:
                    values[af.name] = af.default
                else:
                    values[af.name] = None
                continue

            # Type coercion
            try:
                values[af.name] = _coerce_value(raw, af.python_type)
            except (ValueError, TypeError) as e:
                return {}, f"Invalid value for {af.label}: {e}"

        return values, ""
