"""
Permission checking for HyperApp standalone auth.

Hierarchical RBAC with 4 levels of access control:
1. Model-level permissions (flat)
2. Role hierarchy (groups inherit parent permissions via recursive CTE)
3. Object-level permissions (per-row access control)
4. Conditional rules (is_owner, time_window, ip_range, field_match, custom)
5. Field-level permissions (hidden/readonly/writable per field per role)

Caches per-user per-request. Superuser bypasses all checks.

Usage:
    from hyperdjango.auth.permissions import PermissionChecker, register_rule_type

    checker = PermissionChecker(db)
    await checker.ensure_tables()

    # Model-level (unchanged from flat RBAC)
    if await checker.has_perm(user, "add_product"):
        ...

    # Role hierarchy: admin inherits editor inherits viewer
    admin = await checker.create_group("admin", parent_id=editor.id)

    # Object-level: user can edit specific post
    await checker.grant_object_perm(user_id=1, codename="change_post", model_name="post", object_id="42")
    if await checker.has_object_perm(user, "change_post", "post", "42"):
        ...

    # Conditional rules
    await checker.add_rule(perm_id, "is_owner", {"owner_field": "user_id"}, group_id=editor_id)
    if await checker.has_perm_with_rules(user, "change_post", "post", obj=post, request=request):
        ...

    # Field-level
    await checker.set_field_access("employee", "salary", group_id=viewer_id, access="hidden")
    fields = await checker.get_field_access(user, "employee")
    filtered = await checker.filter_fields(user, "employee", data, mode="read")
"""

import asyncio
import contextlib
import importlib
import inspect
import ipaddress
import secrets
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from hyperdjango.auth.passwords import (
    hash_password,
    needs_rehash,
    verify_password,
)
from hyperdjango.auth.user import (
    CustomRuleConfig,
    FieldMatchConfig,
    FieldPermission,
    Group,
    GroupPermission,
    IpRangeConfig,
    IsOwnerConfig,
    ObjectPermission,
    Permission,
    PermissionRule,
    RBACAuditEntry,
    RuleConfig,
    SessionUser,
    TimeWindowConfig,
    User,
    UserGroup,
    UserPermission,
    ensure_rbac_tables,
    normalize_username,
    rule_config_from_json,
    rule_config_to_dict,
    rule_config_to_json,
)
from hyperdjango.conf import get_setting
from hyperdjango.logging import logger
from hyperdjango.models import Model
from hyperdjango.request import Request
from hyperdjango.tenancy import get_tenant, tenant_context
from hyperdjango.types import PermissionObjDict

# Type for permission-checked objects: Model instances or plain dicts.
# getattr is used because the checked field name (e.g. owner_field) is dynamic
# and varies per rule config — this is genuine meta-programming, not guessing.
PermissionObj = Model | PermissionObjDict

_DEFAULT_ACTIONS = frozenset({"add", "change", "delete", "view"})
_PERM_CACHE_ATTRS = frozenset({"_perm_cache", "_role_tree_cache", "_rules_cache"})
_HIDDEN_LEVELS = frozenset({"hidden", "readonly"})


def _is_superuser(user: User | SessionUser) -> bool:
    """Check if a user is a superuser.

    SessionUser: checks ``user.groups`` frozenset via ``in_group()``.
    User (DB model): checks ``is_superuser`` column property.
    """
    if isinstance(user, SessionUser):
        return user.in_group("superuser")
    return user.is_superuser


def _user_pk(user) -> object | None:
    """Resolve a user object's primary key across the heterogeneous user shapes
    the checker accepts.

    ``user`` is deliberately untyped: callers pass a ``User`` model (has both
    ``.id`` and ``.pk``), a ``SessionUser`` (both, as properties), an
    ``AnonymousUser`` (both, ``None``), or an admin ``UserProxy`` built from a
    raw DB dict which may expose ``.id`` but NOT ``.pk``. The ``.pk`` fallback
    and the ``None`` default are load-bearing for that last case, so reflection
    is genuinely required here.
    """
    # dynamic-attr: user identity spans heterogeneous shapes (Model, SessionUser, AnonymousUser, admin UserProxy) where .pk is not universally present — id→pk fallback with a None default is genuine reflection
    return getattr(user, "id", None) or getattr(user, "pk", None)


# Pre-computed dummy hash for constant-time user enumeration prevention
_DUMMY_HASH = hash_password("dummy-password-for-timing-normalization")

# ── Rule evaluator registry ──────────────────────────────────────────────────

_RULE_EVALUATORS: dict[str, Callable] = {}


def register_rule_type(name: str, evaluator: Callable):
    """Register a custom rule evaluator.

    Evaluator signature: async def eval(user, obj, request, config: dict) -> bool
    """
    _RULE_EVALUATORS[name] = evaluator


# Access level ordering for field permissions (higher = more permissive)
_ACCESS_RANK = {"hidden": 0, "readonly": 1, "writable": 2}


# ── Live session invalidation on RBAC change ─────────────────────────────────
#
# RBAC state (group membership, is_staff/is_superuser, field_access) is
# snapshotted into the session at ``build_session_data()`` time; request guards
# read the frozen session frozenset with NO DB re-check. Without an explicit
# signal, revoking a user's superuser/staff group leaves their EXISTING sessions
# fully privileged until natural expiry (24h) or logout — a de-escalated admin
# keeps full access. The RBAC mutation paths below now emit a revocation signal
# so the change takes effect on the very next request.
#
# Two complementary mechanisms drive that signal:
#
#   1. A per-user "auth epoch": a monotonic counter bumped on every RBAC
#      mutation. A guard that stamps the current epoch into the session at login
#      and re-checks it per request can detect "your roles changed —
#      re-authenticate" WITHOUT needing a handle on any particular session
#      store. This is the store-independent fallback and always fires.
#
#   2. A registry of session-invalidation hooks. Any live session store — the
#      app ``SessionAuth`` store, the admin store, a ``DatabaseSessionStore`` —
#      registers its ``invalidate_for_user`` via
#      ``register_session_invalidation_hook``; on an RBAC mutation every hook is
#      invoked so the user's existing sessions are dropped immediately (real
#      invalidation, preferred over the lazy epoch check).
#
# ``invalidate_user_sessions()`` drives both and is awaited from every mutation
# path that changes a single user's effective authorization.

_auth_epoch: dict[object, int] = {}
_session_invalidation_hooks: list[Callable] = []
# Guards both the epoch map and the hooks list. Registration/mutation are rare;
# under free-threading (no GIL) an unguarded dict/list mutation can corrupt or
# lose an entry, so every read/write of the pair holds this lock.
_invalidation_lock = threading.Lock()


def register_session_invalidation_hook(hook: Callable) -> None:
    """Register a callback invoked as ``hook(user_id)`` on any RBAC change.

    Wire a live session store's ``invalidate_for_user`` so a de-escalated user
    loses access immediately, e.g.::

        register_session_invalidation_hook(session_store.invalidate_for_user)

    The hook may be sync OR async (its result is awaited if it is a
    coroutine/future). Duplicate registrations of the same callable are ignored.
    """
    with _invalidation_lock:
        if hook not in _session_invalidation_hooks:
            _session_invalidation_hooks.append(hook)


def unregister_session_invalidation_hook(hook: Callable) -> None:
    """Remove a previously registered invalidation hook (no-op if absent)."""
    with _invalidation_lock, contextlib.suppress(ValueError):
        _session_invalidation_hooks.remove(hook)


def get_auth_epoch(user_id) -> int:
    """Return the current auth epoch for a user (0 if never bumped).

    A guard stamps this into the session at login and compares it to the live
    value on each request; a mismatch means the user's RBAC state changed and
    the session must be re-established.
    """
    if user_id is None:
        return 0
    with _invalidation_lock:
        return _auth_epoch.get(user_id, 0)


def bump_auth_epoch(user_id) -> int:
    """Increment and return a user's auth epoch, invalidating stamped sessions."""
    if user_id is None:
        return 0
    with _invalidation_lock:
        n = _auth_epoch.get(user_id, 0) + 1
        _auth_epoch[user_id] = n
        return n


async def invalidate_user_sessions(user_id) -> None:
    """Invalidate a user's live sessions after an RBAC change.

    Always bumps the user's auth epoch (store-independent signal), then invokes
    every registered session-invalidation hook (awaiting async ones, e.g. a
    ``DatabaseSessionStore``). Best-effort per hook: a single failing store must
    not stop the others from revoking, and the epoch bump already guarantees a
    guard-checkable signal even if no store is wired.
    """
    if user_id is None:
        return
    bump_auth_epoch(user_id)
    with _invalidation_lock:
        hooks = list(_session_invalidation_hooks)
    for hook in hooks:
        try:
            result = hook(user_id)
            if asyncio.iscoroutine(result) or asyncio.isfuture(result):
                await result
        # A misbehaving store hook must not abort the mutation or block the
        # remaining hooks; the epoch bump above is the guaranteed fallback, so a
        # failing store is logged and skipped, never propagated into the RBAC
        # mutation that triggered it.
        # blind-except: best-effort fan-out to independent session stores
        except Exception as exc:
            logger.opt(exception=True).warning(
                "invalidate_user_sessions: hook {hook!r} failed for "
                "user_id={uid}: {err}",
                hook=hook,
                uid=user_id,
                err=exc,
            )


class RBACauditLog:
    """Tracks all RBAC permission changes for audit trail."""

    def __init__(self, db):
        self.db = db

    async def log(
        self,
        action: str,
        target_type: str,
        target_id: str = "",
        detail: dict | None = None,
        actor_user_id: int | None = None,
        actor_username: str = "",
    ):
        """Record an RBAC change event via ORM."""
        with contextlib.suppress(Exception):
            entry = RBACAuditEntry(
                actor_user_id=actor_user_id,
                actor_username=actor_username,
                action=action,
                target_type=target_type,
                target_id=target_id,
                detail=detail or {},
            )
            await entry.save(db=self.db)

    async def get_recent(self, limit: int = 50) -> list[dict]:
        """Get recent RBAC audit entries."""
        entries = (
            await RBACAuditEntry.objects.using(self.db).order_by("-created_at").all()
        )
        result = []
        for entry in entries[:limit]:
            d = {
                "id": entry.id,
                "actor_user_id": entry.actor_user_id,
                "actor_username": entry.actor_username,
                "action": entry.action,
                "target_type": entry.target_type,
                "target_id": entry.target_id,
                "detail": entry.detail,
                "timestamp": entry.created_at,
            }
            result.append(d)
        return result


class PermissionChecker:
    """Resolves and caches user permissions from the database."""

    def __init__(self, db):
        self.db = db
        self._audit = RBACauditLog(db)

    async def ensure_tables(self):
        """Create auth tables and RBAC indexes if they don't exist.

        Uses create_table_for_model() for all RBAC models — Model definitions
        are the single source of truth for schema. No raw CREATE TABLE SQL.
        """
        await ensure_rbac_tables(db=self.db)

    # ── Permission checking ───────────────────────────────────────────────

    async def has_perm(self, user, perm: str, model_name: str | None = None) -> bool:
        """Check if a user has a specific permission.

        Superuser with is_active=True bypasses all checks.
        Uses cached permissions on the user object when available.

        Codename scoping: pass ``model_name`` (or a fully-qualified
        ``"model.codename"`` as ``perm``) to bind the check to one model. Without
        a model, a bare codename matches ANY model that has it — so a codename
        reused across models (e.g. "publish" on both Article and Invoice) would
        let an Article-publisher pass an Invoice check. Scope the codename to the
        model to avoid this cross-model bleed.
        """
        if not user.is_active:
            return False
        if _is_superuser(user):
            return True

        # A fully-qualified "model.codename" perm scopes itself, even when no
        # explicit model_name is given — match it EXACTLY, never via the
        # any-model fallback below.
        if model_name is None and "." in perm:
            model_name, _, perm = perm.partition(".")

        perms = await self._get_all_permissions(user)
        if model_name:
            return f"{model_name}.{perm}" in perms
        # No model scope — match the codename under any model (may bleed across
        # models sharing a codename; callers scope via model_name to prevent it).
        return any(p.endswith(f".{perm}") or p == perm for p in perms)

    async def has_perms(
        self, user, perm_list: list[str], model_name: str | None = None
    ) -> bool:
        """Check if a user has ALL of the specified permissions."""
        for perm in perm_list:
            if not await self.has_perm(user, perm, model_name):
                return False
        return True

    async def has_model_perms(self, user, model_name: str) -> dict[str, bool]:
        """Get all permission flags for a specific model.

        Returns dict: {"add": bool, "change": bool, "delete": bool, "view": bool}
        """
        return {
            "add": await self.has_perm(user, f"add_{model_name}", model_name),
            "change": await self.has_perm(user, f"change_{model_name}", model_name),
            "delete": await self.has_perm(user, f"delete_{model_name}", model_name),
            "view": await self.has_perm(user, f"view_{model_name}", model_name),
        }

    async def _get_all_permissions(self, user) -> set[str]:
        """Get all permissions for a user (direct + group with hierarchy).

        Uses a recursive CTE to walk the group parent chain, so a user in
        "editor" (child of "viewer") automatically inherits viewer's permissions.
        Cached on user object for request lifetime.
        """
        # Check tenant-aware cache first (cache key includes tenant_id)
        tenant = get_tenant()
        tenant_id = tenant.tenant_id if tenant is not None else None
        cache_key = f"_perm_cache_{tenant_id}"
        # dynamic-attr: cache_key is a per-tenant name computed at runtime (_perm_cache_<tenant_id>); request-lifetime memo stashed on the user under that dynamic key
        cache = getattr(user, cache_key, None)
        if cache is not None:
            return cache

        user_id = _user_pk(user)
        if user_id is None:
            return set()

        # tenant_id and cache_key already resolved above

        # Direct user permissions (global + tenant-scoped)
        if tenant_id is not None:
            direct = await self.db.query(
                "SELECT p.codename, p.model_name FROM hyper_permissions p "
                "JOIN hyper_user_permissions up ON p.id = up.permission_id "
                "WHERE up.user_id = $1 AND (up.tenant_id IS NULL OR up.tenant_id = $2)",
                user_id,
                tenant_id,
            )
        else:
            direct = await self.db.query(
                "SELECT p.codename, p.model_name FROM hyper_permissions p "
                "JOIN hyper_user_permissions up ON p.id = up.permission_id "
                "WHERE up.user_id = $1",
                user_id,
            )

        # Group permissions WITH hierarchical inheritance via recursive CTE.
        # Walks from the user's direct groups up through parent_id chain.
        # Includes both global (tenant_id IS NULL) and tenant-scoped permissions.
        if tenant_id is not None:
            group = await self.db.query(
                "WITH RECURSIVE role_tree AS ("
                "  SELECT group_id AS id FROM hyper_user_groups WHERE user_id = $1 "
                "  UNION ALL "
                "  SELECT g.parent_id FROM hyper_groups g "
                "  JOIN role_tree rt ON g.id = rt.id "
                "  WHERE g.parent_id IS NOT NULL"
                ") "
                "SELECT DISTINCT p.codename, p.model_name FROM hyper_permissions p "
                "JOIN hyper_group_permissions gp ON p.id = gp.permission_id "
                "WHERE gp.group_id IN (SELECT id FROM role_tree) "
                "AND (gp.tenant_id IS NULL OR gp.tenant_id = $2)",
                user_id,
                tenant_id,
            )
        else:
            group = await self.db.query(
                "WITH RECURSIVE role_tree AS ("
                "  SELECT group_id AS id FROM hyper_user_groups WHERE user_id = $1 "
                "  UNION ALL "
                "  SELECT g.parent_id FROM hyper_groups g "
                "  JOIN role_tree rt ON g.id = rt.id "
                "  WHERE g.parent_id IS NOT NULL"
                ") "
                "SELECT DISTINCT p.codename, p.model_name FROM hyper_permissions p "
                "JOIN hyper_group_permissions gp ON p.id = gp.permission_id "
                "WHERE gp.group_id IN (SELECT id FROM role_tree)",
                user_id,
            )

        perms = set()
        for row in direct:
            codename = row["codename"] if isinstance(row, dict) else row[0]
            model = row["model_name"] if isinstance(row, dict) else row[1]
            perms.add(f"{model}.{codename}")
        for row in group:
            codename = row["codename"] if isinstance(row, dict) else row[0]
            model = row["model_name"] if isinstance(row, dict) else row[1]
            perms.add(f"{model}.{codename}")

        # dynamic-attr: memoize the resolved permission set on the user object under the per-tenant cache_key computed above
        setattr(user, cache_key, perms)
        return perms

    def clear_cache(self, user):
        """Clear ALL permission caches on a user object.

        Clears: model-level perms, role tree, rule results, field access.
        Call after any permission grant/revoke operation.
        """
        for attr in list(vars(user)) if hasattr(user, "__dict__") else []:
            if (
                attr.startswith("_perm_cache")
                or attr.startswith("_role_tree")
                or attr.startswith("_rules_cache")
                or attr.startswith("_field_access_")
            ):
                delattr(user, attr)
        # Also clear specific known attrs (for objects without __dict__)
        for attr in _PERM_CACHE_ATTRS:
            if hasattr(user, attr):
                with contextlib.suppress(AttributeError):
                    delattr(user, attr)

    async def _get_role_tree(self, user) -> list[int]:
        """Get all group IDs in the user's role hierarchy (direct + ancestors).

        Cached on user._role_tree_cache for request lifetime.
        Used by object perms, rules, and field perms to avoid re-executing CTE.
        """
        # dynamic-attr: optional request-lifetime cache attr stashed on the user object (absent on first access; not a declared field; user type varies)
        cached = getattr(user, "_role_tree_cache", None)
        if cached is not None:
            return cached

        user_id = _user_pk(user)
        if user_id is None:
            return []

        rows = await self.db.query(
            "WITH RECURSIVE role_tree AS ("
            "  SELECT group_id AS id FROM hyper_user_groups WHERE user_id = $1 "
            "  UNION ALL "
            "  SELECT g.parent_id FROM hyper_groups g "
            "  JOIN role_tree rt ON g.id = rt.id "
            "  WHERE g.parent_id IS NOT NULL"
            ") SELECT id FROM role_tree",
            user_id,
        )
        tree = [r["id"] if isinstance(r, dict) else r[0] for r in rows]
        with contextlib.suppress(AttributeError, TypeError):
            user._role_tree_cache = tree
        return tree

    async def _get_role_tree_by_id(self, user_id: int) -> list[int]:
        """Get all group IDs in a user's role hierarchy by user_id directly.

        Same CTE as ``_get_role_tree`` but without requiring a user object.
        Used by ``get_all_field_access()`` during session population.
        """
        rows = await self.db.query(
            "WITH RECURSIVE role_tree AS ("
            "  SELECT group_id AS id FROM hyper_user_groups WHERE user_id = $1 "
            "  UNION ALL "
            "  SELECT g.parent_id FROM hyper_groups g "
            "  JOIN role_tree rt ON g.id = rt.id "
            "  WHERE g.parent_id IS NOT NULL"
            ") SELECT id FROM role_tree",
            user_id,
        )
        return [r["id"] if isinstance(r, dict) else r[0] for r in rows]

    # ── Permission management ─────────────────────────────────────────────

    def _perm_qs(self) -> QuerySet:
        """Permission QuerySet bound to self.db."""
        return Permission.objects.using(self.db)

    def _user_perm_qs(self) -> QuerySet:
        """UserPermission QuerySet bound to self.db."""
        return UserPermission.objects.using(self.db)

    def _group_perm_qs(self) -> QuerySet:
        """GroupPermission QuerySet bound to self.db."""
        return GroupPermission.objects.using(self.db)

    def _obj_perm_qs(self) -> QuerySet:
        """ObjectPermission QuerySet bound to self.db."""
        return ObjectPermission.objects.using(self.db)

    def _field_perm_qs(self) -> QuerySet:
        """FieldPermission QuerySet bound to self.db."""
        return FieldPermission.objects.using(self.db)

    def _rule_qs(self) -> QuerySet:
        """PermissionRule QuerySet bound to self.db."""
        return PermissionRule.objects.using(self.db)

    async def _resolve_perm(self, codename: str, model_name: str) -> Permission | None:
        """Look up a Permission by codename + model_name."""
        return (
            await self._perm_qs()
            .filter(codename=codename, model_name=model_name)
            .first()
        )

    async def create_default_permissions(self, model_name: str, verbose_name: str):
        """Create the 4 default permissions for a model (add, change, delete, view)."""
        for action in _DEFAULT_ACTIONS:
            codename = f"{action}_{model_name}"
            name = f"Can {action} {verbose_name}"
            existing = (
                await self._perm_qs()
                .filter(codename=codename, model_name=model_name)
                .first()
            )
            if existing is None:
                perm = Permission(codename=codename, name=name, model_name=model_name)
                await perm.save(db=self.db)

    async def grant_user_perm(self, user_id: int, codename: str, model_name: str):
        """Grant a permission directly to a user."""
        perm = await self._resolve_perm(codename, model_name)
        if perm is None:
            return
        existing = (
            await self._user_perm_qs()
            .filter(user_id=user_id, permission_id=perm.id)
            .first()
        )
        if existing is None:
            up = UserPermission(user_id=user_id, permission_id=perm.id)
            await up.save(db=self.db)
        await self._audit.log(
            "grant_perm",
            "user",
            str(user_id),
            {"codename": codename, "model": model_name},
        )
        # The user's effective permission set changed — drop their live sessions
        # so the new grant is reflected on the next request (sessions cache perms
        # at build_session_data() time and never re-check).
        await invalidate_user_sessions(user_id)

    async def revoke_user_perm(self, user_id: int, codename: str, model_name: str):
        """Revoke a permission from a user."""
        perm = await self._resolve_perm(codename, model_name)
        if perm is not None:
            await (
                self._user_perm_qs()
                .filter(user_id=user_id, permission_id=perm.id)
                .delete()
            )
        await self._audit.log(
            "revoke_perm",
            "user",
            str(user_id),
            {"codename": codename, "model": model_name},
        )
        # De-escalation: invalidate live sessions so the revoked permission stops
        # working immediately rather than lingering until session expiry.
        await invalidate_user_sessions(user_id)

    async def grant_group_perm(self, group_id: int, codename: str, model_name: str):
        """Grant a permission to a group."""
        perm = await self._resolve_perm(codename, model_name)
        if perm is None:
            return
        existing = (
            await self._group_perm_qs()
            .filter(group_id=group_id, permission_id=perm.id)
            .first()
        )
        if existing is None:
            gp = GroupPermission(group_id=group_id, permission_id=perm.id)
            await gp.save(db=self.db)
        await self._audit.log(
            "grant_perm",
            "group",
            str(group_id),
            {"codename": codename, "model": model_name},
        )

    async def add_user_to_group(self, user_id: int, group_id: int):
        """Add a user to a group."""
        existing = (
            await self._user_group_qs()
            .filter(user_id=user_id, group_id=group_id)
            .first()
        )
        if existing is None:
            ug = UserGroup(user_id=user_id, group_id=group_id)
            await ug.save(db=self.db)
        await self._audit.log(
            "add_to_group", "user", str(user_id), {"group_id": group_id}
        )
        # Group membership drives is_staff/is_superuser/perms in the session
        # snapshot — invalidate live sessions so the change (escalation OR later
        # de-escalation) is picked up on the next request, not at expiry.
        await invalidate_user_sessions(user_id)

    async def remove_user_from_group(self, user_id: int, group_id: int):
        """Remove a user from a group."""
        await self._user_group_qs().filter(user_id=user_id, group_id=group_id).delete()
        await self._audit.log(
            "remove_from_group", "user", str(user_id), {"group_id": group_id}
        )
        # SECURITY (live de-escalation): removing a user from a privileged group
        # (e.g. "superuser"/"staff") must revoke their EXISTING sessions now.
        # Sessions freeze group membership at login; without this the demoted
        # admin keeps full access until the 24h session expiry or a manual
        # logout. invalidate_user_sessions() bumps the auth epoch AND fires every
        # registered store hook (real invalidation).
        await invalidate_user_sessions(user_id)

    def _group_qs(self) -> QuerySet:
        """Group QuerySet bound to self.db."""
        return Group.objects.using(self.db)

    def _user_group_qs(self) -> QuerySet:
        """UserGroup QuerySet bound to self.db."""
        return UserGroup.objects.using(self.db)

    async def get_user_groups(self, user_id: int) -> list[Group]:
        """Get all groups a user belongs to (direct membership only).

        Returns list of Group model instances ordered by priority (highest first).
        """
        memberships = (
            await self._user_group_qs()
            .select_related("group_id")
            .filter(user_id=user_id)
            .all()
        )
        groups = [m.group_id for m in memberships if m.group_id is not None]
        groups.sort(key=lambda g: g.priority, reverse=True)
        return groups

    async def get_user_group_names(self, user_id: int) -> list[str]:
        """Get all group names for a user (direct membership).

        Returns sorted list of group names. Use for storing in session data.
        """
        groups = await self.get_user_groups(user_id)
        names = [g.name for g in groups]
        names.sort()
        return names

    async def is_in_group(self, user_id: int, group_name: str) -> bool:
        """Check if a user is a member of a named group (direct membership)."""
        memberships = (
            await self._user_group_qs()
            .select_related("group_id")
            .filter(user_id=user_id)
            .all()
        )
        return any(
            m.group_id is not None and m.group_id.name == group_name
            for m in memberships
        )

    async def get_group_by_name(self, name: str) -> Group | None:
        """Get a group by name. Returns Group model instance or None."""
        return await self._group_qs().filter(name=name).first()

    # ── User management ──────────────────────────────────────────────────

    def _user_qs(self) -> QuerySet:
        """User QuerySet bound to self.db for all ORM operations."""
        return User.objects.using(self.db)

    async def create_user(
        self,
        username: str,
        password: str,
        email: str = "",
        is_staff: bool = False,
        is_superuser: bool = False,
        first_name: str = "",
        last_name: str = "",
    ) -> User:
        """Create a new user via ORM. Returns the User model instance."""
        user = User(
            username=username,
            email=email,
            password_hash=hash_password(password),
            first_name=first_name,
            last_name=last_name,
            is_staff=is_staff,
            is_superuser=is_superuser,
        )
        await user.save(db=self.db)
        return user

    async def ensure_admin_user(
        self,
        username: str = "admin",
        password: str | None = None,
        email: str = "admin@example.com",
    ) -> User:
        """Get or create an admin user with staff+superuser RBAC groups.

        Idempotent — returns the existing user if one with this username exists.

        Password resolution (in order):
        1. Explicit ``password`` parameter
        2. ``HYPER_ADMIN_PASSWORD`` environment variable
        3. Random generated (printed to stdout so operator can record it)

        Used by seed files to ensure HyperAdmin panel access.
        """
        # Canonicalize the lookup key to match the NFKC-normalized value that
        # the write path (User.model_post_init) stores, so NFC/NFD spellings
        # of the same name resolve to the one account.
        existing = (
            await self._user_qs().filter(username=normalize_username(username)).first()
        )
        if existing is not None:
            return existing
        if password is None:
            password = get_setting("ADMIN_PASSWORD")
        if not password:
            password = secrets.token_urlsafe(16)
            logger.warning(
                "Generated admin password for '{username}': {password}  "
                "(set HYPER_ADMIN_PASSWORD env var to control this)",
                username=username,
                password=password,
            )
        user = await self.create_user(
            username, password, email=email, is_staff=True, is_superuser=True
        )
        staff_group = await self.ensure_group("staff")
        su_group = await self.ensure_group("superuser")
        await self.add_user_to_group(user.id, staff_group.id)
        await self.add_user_to_group(user.id, su_group.id)
        return user

    async def authenticate(
        self, username: str, password: str
    ) -> dict[str, str | int | bool | None] | None:
        """Authenticate a user by username and password.

        Returns a user info dict on success, None on failure.
        Re-hashes password if argon2 parameters have been upgraded.
        """
        user = (
            await self._user_qs()
            .filter(username=normalize_username(username), is_active=True)
            .first()
        )
        if user is None:
            # Constant-time: run a dummy verify to prevent user enumeration via timing
            verify_password(password, _DUMMY_HASH)
            return None

        if not verify_password(password, user.password_hash):
            return None

        # Transparent rehash if parameters upgraded
        if needs_rehash(user.password_hash):
            user.password_hash = hash_password(password)

        # Update last_login
        user.last_login = datetime.now(UTC)
        await user.save(db=self.db)

        return user.to_dict()

    async def get_user_by_id(
        self, user_id: int
    ) -> dict[str, str | int | bool | None] | None:
        """Fetch a user by ID. Returns dict (password_hash excluded by default) or None."""
        user = await self._user_qs().filter(id=user_id).first()
        if user is None:
            return None
        return user.to_dict()

    # ── Group management (with hierarchy) ───────────────────────────────

    async def create_group(
        self, name: str, parent_id: int | None = None, priority: int = 0
    ) -> Group:
        """Create a group/role. Returns the Group model instance.

        parent_id: inherit all permissions from the parent group chain.
        priority: higher = more authoritative when resolving conflicts.
        """
        group = Group(name=name, parent_id=parent_id, priority=priority)
        await group.save(db=self.db)
        await self._audit.log(
            "create_group",
            "group",
            str(group.id),
            {"name": name, "parent_id": parent_id, "priority": priority},
        )
        return group

    async def ensure_group(self, name: str) -> Group:
        """Get or create a group by name. Idempotent.

        Returns the existing group if one with this name already exists,
        otherwise creates a new one. Used by ``createsuperuser`` and
        ``hyper setup`` to ensure "staff" and "superuser" groups exist.
        """
        existing = await self._group_qs().filter(name=name).first()
        if existing is not None:
            return existing
        return await self.create_group(name)

    async def set_group_parent(self, group_id: int, parent_id: int | None):
        """Set or change a group's parent. Detects cycles."""
        if parent_id is not None:
            ancestors = await self.get_role_ancestors(parent_id)
            if group_id in ancestors:
                raise ValueError(
                    f"Cycle detected: group {group_id} is already an ancestor of {parent_id}"
                )
        await self._group_qs().filter(id=group_id).update(parent_id=parent_id)
        await self._audit.log(
            "set_parent", "group", str(group_id), {"parent_id": parent_id}
        )

    async def get_role_ancestors(self, group_id: int) -> list[int]:
        """Get all ancestor group IDs (including self) via recursive CTE."""
        rows = await self.db.query(
            "WITH RECURSIVE ancestors AS ("
            "  SELECT id, parent_id FROM hyper_groups WHERE id = $1 "
            "  UNION ALL "
            "  SELECT g.id, g.parent_id FROM hyper_groups g "
            "  JOIN ancestors a ON g.id = a.parent_id"
            ") "
            "SELECT id FROM ancestors",
            group_id,
        )
        return [r["id"] if isinstance(r, dict) else r[0] for r in rows]

    async def get_group_children(self, group_id: int) -> list[Group]:
        """Get direct child groups ordered by priority (highest first)."""
        return (
            await self._group_qs()
            .filter(parent_id=group_id)
            .order_by("-priority")
            .all()
        )

    # ── Object-level permissions ──────────────────────────────────────────

    async def has_object_perm(
        self, user, perm: str, model_name: str, object_id: str
    ) -> bool:
        """Check if user has permission on a specific object.

        Resolution order:
        1. Superuser bypass
        2. Model-level perm (grants access to ALL objects)
        3. Direct user object perm
        4. Group object perm (with hierarchy)
        """
        if not user.is_active:
            return False
        if _is_superuser(user):
            return True

        # Model-level perm grants access to all objects
        if await self.has_perm(user, perm, model_name):
            return True

        user_id = _user_pk(user)
        if user_id is None:
            return False

        # Use cached role tree instead of re-executing CTE
        role_tree = await self._get_role_tree(user)
        if role_tree:
            placeholders = ", ".join(f"${i + 5}" for i in range(len(role_tree)))
            row = await self.db.query_one(
                "SELECT 1 FROM hyper_object_permissions op "
                "JOIN hyper_permissions p ON op.permission_id = p.id "
                "WHERE p.codename = $2 AND op.object_model = $3 AND op.object_id = $4 "
                f"AND (op.user_id = $1 OR op.group_id IN ({placeholders})) LIMIT 1",
                user_id,
                perm,
                model_name,
                str(object_id),
                *role_tree,
            )
        else:
            row = await self.db.query_one(
                "SELECT 1 FROM hyper_object_permissions op "
                "JOIN hyper_permissions p ON op.permission_id = p.id "
                "WHERE p.codename = $2 AND op.object_model = $3 AND op.object_id = $4 "
                "AND op.user_id = $1 LIMIT 1",
                user_id,
                perm,
                model_name,
                str(object_id),
            )
        return row is not None

    async def grant_object_perm(
        self,
        codename: str,
        model_name: str,
        object_id: str,
        user_id: int | None = None,
        group_id: int | None = None,
    ):
        """Grant a permission on a specific object to a user or group."""
        if user_id is not None:
            await self.db.execute(
                "INSERT INTO hyper_object_permissions (user_id, permission_id, object_model, object_id) "
                "SELECT $1, p.id, $5, $4 FROM hyper_permissions p "
                "WHERE p.codename = $2 AND p.model_name = $3 "
                "ON CONFLICT DO NOTHING",
                user_id,
                codename,
                model_name,
                str(object_id),
                model_name,
            )
        elif group_id is not None:
            await self.db.execute(
                "INSERT INTO hyper_object_permissions (group_id, permission_id, object_model, object_id) "
                "SELECT $1, p.id, $5, $4 FROM hyper_permissions p "
                "WHERE p.codename = $2 AND p.model_name = $3 "
                "ON CONFLICT DO NOTHING",
                group_id,
                codename,
                model_name,
                str(object_id),
                model_name,
            )
        await self._audit.log(
            "grant_object_perm",
            "object",
            str(object_id),
            {
                "codename": codename,
                "model": model_name,
                "user_id": user_id,
                "group_id": group_id,
            },
        )

    async def revoke_object_perm(
        self,
        codename: str,
        model_name: str,
        object_id: str,
        user_id: int | None = None,
        group_id: int | None = None,
    ):
        """Revoke a permission on a specific object."""
        if user_id is not None:
            await self.db.execute(
                "DELETE FROM hyper_object_permissions WHERE user_id = $1 "
                "AND permission_id = (SELECT id FROM hyper_permissions WHERE codename = $2 AND model_name = $3) "
                "AND object_model = $3 AND object_id = $4",
                user_id,
                codename,
                model_name,
                str(object_id),
            )
        elif group_id is not None:
            await self.db.execute(
                "DELETE FROM hyper_object_permissions WHERE group_id = $1 "
                "AND permission_id = (SELECT id FROM hyper_permissions WHERE codename = $2 AND model_name = $3) "
                "AND object_model = $3 AND object_id = $4",
                group_id,
                codename,
                model_name,
                str(object_id),
            )
        await self._audit.log(
            "revoke_object_perm",
            "object",
            str(object_id),
            {
                "codename": codename,
                "model": model_name,
                "user_id": user_id,
                "group_id": group_id,
            },
        )

    async def get_objects_with_perm(
        self, user, codename: str, model_name: str
    ) -> list[str]:
        """Get all object IDs the user has a specific permission on."""
        user_id = _user_pk(user)
        if user_id is None:
            return []
        role_tree = await self._get_role_tree(user)
        if role_tree:
            placeholders = ", ".join(f"${i + 4}" for i in range(len(role_tree)))
            rows = await self.db.query(
                "SELECT DISTINCT op.object_id FROM hyper_object_permissions op "
                "JOIN hyper_permissions p ON op.permission_id = p.id "
                "WHERE p.codename = $2 AND op.object_model = $3 "
                f"AND (op.user_id = $1 OR op.group_id IN ({placeholders}))",
                user_id,
                codename,
                model_name,
                *role_tree,
            )
        else:
            rows = await self.db.query(
                "SELECT DISTINCT op.object_id FROM hyper_object_permissions op "
                "JOIN hyper_permissions p ON op.permission_id = p.id "
                "WHERE p.codename = $2 AND op.object_model = $3 AND op.user_id = $1",
                user_id,
                codename,
                model_name,
            )
        return [r["object_id"] if isinstance(r, dict) else r[0] for r in rows]

    # ── Conditional rules ─────────────────────────────────────────────────

    async def add_rule(
        self,
        codename: str,
        model_name: str,
        rule_type: str,
        rule_config: RuleConfig,
        group_id: int | None = None,
        user_id: int | None = None,
        priority: int = 0,
        is_deny: bool = False,
    ):
        """Add a conditional rule to a permission."""
        config_json = rule_config_to_json(rule_config)
        await self.db.execute(
            "INSERT INTO hyper_permission_rules (permission_id, rule_type, rule_config, "
            "group_id, user_id, priority, is_deny) "
            "SELECT p.id, $2, $3, $4, $5, $6, $7 FROM hyper_permissions p "
            "WHERE p.codename = $1 AND p.model_name = $8",
            codename,
            rule_type,
            config_json,
            group_id,
            user_id,
            priority,
            is_deny,
            model_name,
        )
        await self._audit.log(
            "add_rule",
            "rule",
            f"{codename}.{model_name}",
            {
                "rule_type": rule_type,
                "is_deny": is_deny,
                "group_id": group_id,
                "user_id": user_id,
            },
        )

    async def has_perm_with_rules(
        self,
        user,
        perm: str,
        model_name: str,
        obj: PermissionObj | None = None,
        request: Request | None = None,
    ) -> bool:
        """Full permission check: model-level + object-level + conditional rules.

        This is the highest-level check combining all RBAC layers.
        """
        if not user.is_active:
            return False
        if _is_superuser(user):
            return True

        # Check model-level permission first
        has_model = await self.has_perm(user, perm, model_name)

        # Load applicable rules (cached per user+perm for request lifetime)
        rules = await self._load_rules(user, perm, model_name)

        if not rules:
            # No rules defined — fall back to model-level perm
            return has_model

        # Evaluate deny rules first (highest priority)
        deny_rules = [r for r in rules if r["is_deny"]]
        allow_rules = [r for r in rules if not r["is_deny"]]

        for rule in deny_rules:
            result = await self._evaluate_rule(user, obj, request, rule)
            # A deny rule that MATCHES denies. A deny rule that is INAPPLICABLE
            # (missing obj/request) also denies — fail closed. Otherwise a
            # non-firing deny in a deny-only ruleset would fall through to the
            # `return has_model` allow below, exactly the bypass this guards.
            if result is INAPPLICABLE or result:
                return False  # Explicit deny (or unevaluatable deny → fail closed)

        # If there are allow rules, at least one must DEFINITELY match.
        # INAPPLICABLE never grants (only a real match does).
        if allow_rules:
            for rule in allow_rules:
                if await self._evaluate_rule(user, obj, request, rule) is True:
                    return True
            return False  # Allow rules exist but none matched

        return has_model

    async def _load_rules(self, user, codename: str, model_name: str) -> list[dict]:
        """Load all rules applicable to this user for this permission.

        Cached on user._rules_cache[codename.model_name] for request lifetime.
        Uses cached role tree to avoid re-executing CTE.
        """
        cache_key = f"{codename}.{model_name}"
        # dynamic-attr: optional request-lifetime cache attr stashed on the user object; absent until first populated, user type varies
        rules_cache = getattr(user, "_rules_cache", None)
        if rules_cache is not None and cache_key in rules_cache:
            return rules_cache[cache_key]

        user_id = _user_pk(user)
        if user_id is None:
            return []

        # Use cached role tree instead of inline CTE
        role_tree = await self._get_role_tree(user)
        if role_tree:
            placeholders = ", ".join(f"${i + 4}" for i in range(len(role_tree)))
            rows = await self.db.query(
                "SELECT r.rule_type, r.rule_config, r.is_deny, r.priority "
                "FROM hyper_permission_rules r "
                "JOIN hyper_permissions p ON r.permission_id = p.id "
                "WHERE p.codename = $2 AND p.model_name = $3 "
                "AND ("
                f"  r.user_id = $1 OR r.group_id IN ({placeholders})"
                "  OR (r.user_id IS NULL AND r.group_id IS NULL)"
                ") "
                "ORDER BY r.is_deny DESC, r.priority DESC",
                user_id,
                codename,
                model_name,
                *role_tree,
            )
        else:
            rows = await self.db.query(
                "SELECT r.rule_type, r.rule_config, r.is_deny, r.priority "
                "FROM hyper_permission_rules r "
                "JOIN hyper_permissions p ON r.permission_id = p.id "
                "WHERE p.codename = $2 AND p.model_name = $3 "
                "AND (r.user_id = $1 OR (r.user_id IS NULL AND r.group_id IS NULL)) "
                "ORDER BY r.is_deny DESC, r.priority DESC",
                user_id,
                codename,
                model_name,
            )

        result = []
        cols = ["rule_type", "rule_config", "is_deny", "priority"]
        for row in rows:
            d = dict(zip(cols, row)) if not isinstance(row, dict) else row
            d["rule_config"] = rule_config_from_json(d["rule_type"], d["rule_config"])
            result.append(d)

        # Cache on user object
        if rules_cache is None:
            rules_cache = {}
            with contextlib.suppress(AttributeError, TypeError):
                user._rules_cache = rules_cache
        rules_cache[cache_key] = result
        return result

    async def _evaluate_rule(
        self,
        user,
        obj: PermissionObj | None,
        request: Request | None,
        rule: dict[str, str | int | bool | RuleConfig],
    ) -> bool | object:
        """Evaluate a single rule against the current context.

        Returns True/False for a definite match/no-match, or the INAPPLICABLE
        sentinel when a built-in evaluator lacks the context (obj/request) to
        decide. Callers must treat INAPPLICABLE distinctly (fail closed for deny,
        never-grant for allow).
        """
        rule_type = rule["rule_type"]
        config = rule["rule_config"]

        # Check registry first (custom rules — supports both sync and async)
        evaluator = _RULE_EVALUATORS.get(rule_type)
        if evaluator is not None:
            if inspect.iscoroutinefunction(evaluator):
                return await evaluator(user, obj, request, config)
            return evaluator(user, obj, request, config)

        # Built-in evaluators
        if rule_type == "is_owner":
            return _eval_is_owner(user, obj, config)
        if rule_type == "time_window":
            return _eval_time_window(config)
        if rule_type == "ip_range":
            return _eval_ip_range(request, config)
        if rule_type == "field_match":
            return _eval_field_match(obj, config)
        if rule_type == "custom":
            return await _eval_custom(user, obj, request, config)

        return False  # Unknown rule type — deny by default

    # ── Field-level permissions ───────────────────────────────────────────

    async def set_field_access(
        self,
        model_name: str,
        field_name: str,
        access: str = "hidden",
        group_id: int | None = None,
        user_id: int | None = None,
    ):
        """Set field access level for a group or user.

        access: "hidden" | "readonly" | "writable"
        """
        if group_id is not None:
            await self.db.execute(
                "INSERT INTO hyper_field_permissions (model_name, field_name, group_id, access) "
                "VALUES ($1, $2, $3, $4) "
                "ON CONFLICT DO NOTHING",
                model_name,
                field_name,
                group_id,
                access,
            )
        elif user_id is not None:
            await self.db.execute(
                "INSERT INTO hyper_field_permissions (model_name, field_name, user_id, access) "
                "VALUES ($1, $2, $3, $4) "
                "ON CONFLICT DO NOTHING",
                model_name,
                field_name,
                user_id,
                access,
            )
        await self._audit.log(
            "set_field_access",
            "field",
            f"{model_name}.{field_name}",
            {"access": access, "group_id": group_id, "user_id": user_id},
        )

    async def get_field_access(self, user, model_name: str) -> dict[str, str]:
        """Get field access levels for a user on a model.

        Returns {field_name: "hidden"|"readonly"|"writable"} for restricted fields.
        Fields not in the result are writable (default permissive).
        When multiple access levels apply, the most permissive wins.
        Cached per request on user._field_access_{model}.
        """
        cache_key = f"_field_access_{model_name}"
        # dynamic-attr: cache_key is a per-model name computed at runtime (_field_access_<model>); read the request-lifetime memo off the user
        cached = getattr(user, cache_key, None)
        if cached is not None:
            return cached

        if _is_superuser(user):
            # dynamic-attr: memoize the empty (superuser = unrestricted) map under the per-model dynamic cache key
            setattr(user, cache_key, {})
            return {}

        user_id = _user_pk(user)
        if user_id is None:
            return {}

        role_tree = await self._get_role_tree(user)
        if role_tree:
            placeholders = ", ".join(f"${i + 3}" for i in range(len(role_tree)))
            rows = await self.db.query(
                "SELECT fp.field_name, fp.access FROM hyper_field_permissions fp "
                "WHERE fp.model_name = $2 "
                f"AND (fp.user_id = $1 OR fp.group_id IN ({placeholders}))",
                user_id,
                model_name,
                *role_tree,
            )
        else:
            rows = await self.db.query(
                "SELECT fp.field_name, fp.access FROM hyper_field_permissions fp "
                "WHERE fp.model_name = $2 AND fp.user_id = $1",
                user_id,
                model_name,
            )

        # Most permissive wins when multiple entries exist for same field
        access_map: dict[str, str] = {}
        for row in rows:
            field = row["field_name"] if isinstance(row, dict) else row[0]
            level = row["access"] if isinstance(row, dict) else row[1]
            existing = access_map.get(field)
            if existing is None or _ACCESS_RANK.get(level, 0) > _ACCESS_RANK.get(
                existing, 0
            ):
                access_map[field] = level

        with contextlib.suppress(AttributeError, TypeError):
            # dynamic-attr: memoize the resolved field-access map under the per-model dynamic cache key on the user object
            setattr(user, cache_key, access_map)
        return access_map

    async def get_all_field_access(self, user_id: int) -> dict[str, dict[str, str]]:
        """Get all field access levels for a user across all models.

        Returns ``{model_name: {field_name: "hidden"|"readonly"|"writable"}}``
        for all restricted fields. Used by ``build_session_data()`` to cache
        field permissions in the session at login time.
        """
        role_tree = await self._get_role_tree_by_id(user_id)
        if role_tree:
            placeholders = ", ".join(f"${i + 2}" for i in range(len(role_tree)))
            rows = await self.db.query(
                "SELECT fp.model_name, fp.field_name, fp.access "
                "FROM hyper_field_permissions fp "
                f"WHERE fp.user_id = $1 OR fp.group_id IN ({placeholders})",
                user_id,
                *role_tree,
            )
        else:
            rows = await self.db.query(
                "SELECT fp.model_name, fp.field_name, fp.access "
                "FROM hyper_field_permissions fp WHERE fp.user_id = $1",
                user_id,
            )

        result: dict[str, dict[str, str]] = {}
        for row in rows:
            model = row["model_name"] if isinstance(row, dict) else row[0]
            field = row["field_name"] if isinstance(row, dict) else row[1]
            level = row["access"] if isinstance(row, dict) else row[2]
            model_map = result.setdefault(model, {})
            existing = model_map.get(field)
            if existing is None or _ACCESS_RANK.get(level, 0) > _ACCESS_RANK.get(
                existing, 0
            ):
                model_map[field] = level
        return result

    async def filter_fields(
        self, user, model_name: str, data: dict[str, object], mode: str = "read"
    ) -> dict[str, object]:
        """Filter a data dict based on field-level permissions.

        mode="read": remove hidden fields
        mode="write": remove hidden + readonly fields
        """
        access = await self.get_field_access(user, model_name)
        if not access:
            return data  # No restrictions

        result = {}
        for key, value in data.items():
            level = access.get(key, "writable")
            if mode == "read" and level == "hidden":
                continue
            if mode == "write" and level in _HIDDEN_LEVELS:
                continue
            result[key] = value
        return result

    # ── Explain / audit methods ─────────────────────────────────────────

    async def explain_effective_permissions(self, user_id: int) -> dict[str, object]:
        """Build a complete permission picture for a user.

        Returns all permissions with source attribution — where each perm
        came from (direct, group name, inherited via chain).
        Used by admin "Effective Permissions" view.
        """
        user_info = await self.get_user_by_id(user_id)
        if user_info is None:
            return {"user": None}

        # Groups with names
        groups = await self.db.query(
            "SELECT g.id, g.name, g.parent_id, g.priority "
            "FROM hyper_groups g "
            "JOIN hyper_user_groups ug ON g.id = ug.group_id "
            "WHERE ug.user_id = $1 ORDER BY g.priority DESC",
            user_id,
        )
        group_list = []
        for r in groups:
            g = (
                dict(zip(["id", "name", "parent_id", "priority"], r))
                if not isinstance(r, dict)
                else r
            )
            group_list.append(g)

        # Direct permissions with source
        direct_rows = await self.db.query(
            "SELECT p.codename, p.model_name, p.name "
            "FROM hyper_permissions p "
            "JOIN hyper_user_permissions up ON p.id = up.permission_id "
            "WHERE up.user_id = $1",
            user_id,
        )
        direct_perms = []
        for r in direct_rows:
            d = (
                dict(zip(["codename", "model_name", "name"], r))
                if not isinstance(r, dict)
                else r
            )
            d["source"] = "direct"
            d["via"] = ""
            direct_perms.append(d)

        # Group permissions with source (including hierarchy)
        group_rows = await self.db.query(
            "WITH RECURSIVE role_tree AS ("
            "  SELECT ug.group_id AS id, g.name AS group_name, CAST(g.name AS TEXT) AS path "
            "  FROM hyper_user_groups ug "
            "  JOIN hyper_groups g ON g.id = ug.group_id "
            "  WHERE ug.user_id = $1 "
            "  UNION ALL "
            "  SELECT g.parent_id, g2.name, rt.path || ' → ' || g2.name "
            "  FROM hyper_groups g "
            "  JOIN role_tree rt ON g.id = rt.id "
            "  JOIN hyper_groups g2 ON g2.id = g.parent_id "
            "  WHERE g.parent_id IS NOT NULL"
            ") "
            "SELECT DISTINCT p.codename, p.model_name, p.name, rt.group_name, rt.path "
            "FROM hyper_permissions p "
            "JOIN hyper_group_permissions gp ON p.id = gp.permission_id "
            "JOIN role_tree rt ON gp.group_id = rt.id",
            user_id,
        )
        inherited_perms = []
        for r in group_rows:
            d = (
                dict(zip(["codename", "model_name", "name", "group_name", "path"], r))
                if not isinstance(r, dict)
                else r
            )
            d["source"] = f"group:{d['group_name']}"
            d["via"] = d.get("path", "")
            inherited_perms.append(d)

        # Object permissions
        obj_rows = await self.db.query(
            "WITH RECURSIVE role_tree AS ("
            "  SELECT group_id AS id FROM hyper_user_groups WHERE user_id = $1 "
            "  UNION ALL "
            "  SELECT g.parent_id FROM hyper_groups g "
            "  JOIN role_tree rt ON g.id = rt.id "
            "  WHERE g.parent_id IS NOT NULL"
            ") "
            "SELECT p.codename, p.model_name, op.object_id, "
            "CASE WHEN op.user_id IS NOT NULL THEN 'direct' ELSE 'group' END AS source "
            "FROM hyper_object_permissions op "
            "JOIN hyper_permissions p ON op.permission_id = p.id "
            "WHERE op.user_id = $1 OR op.group_id IN (SELECT id FROM role_tree)",
            user_id,
        )
        object_perms = []
        for r in obj_rows:
            d = (
                dict(zip(["codename", "model_name", "object_id", "source"], r))
                if not isinstance(r, dict)
                else r
            )
            object_perms.append(d)

        # Rules (with hierarchy)
        rule_rows = await self.db.query(
            "WITH RECURSIVE role_tree AS ("
            "  SELECT group_id AS id FROM hyper_user_groups WHERE user_id = $1 "
            "  UNION ALL "
            "  SELECT g.parent_id FROM hyper_groups g "
            "  JOIN role_tree rt ON g.id = rt.id "
            "  WHERE g.parent_id IS NOT NULL"
            ") "
            "SELECT p.codename, p.model_name, r.rule_type, r.rule_config, r.is_deny, r.priority, "
            "CASE WHEN r.user_id IS NOT NULL THEN 'direct' "
            "WHEN r.group_id IS NOT NULL THEN 'group' ELSE 'global' END AS scope "
            "FROM hyper_permission_rules r "
            "JOIN hyper_permissions p ON r.permission_id = p.id "
            "WHERE r.user_id = $1 OR r.group_id IN (SELECT id FROM role_tree) "
            "OR (r.user_id IS NULL AND r.group_id IS NULL) "
            "ORDER BY r.is_deny DESC, r.priority DESC",
            user_id,
        )
        rules = []
        for r in rule_rows:
            d = (
                dict(
                    zip(
                        [
                            "codename",
                            "model_name",
                            "rule_type",
                            "rule_config",
                            "is_deny",
                            "priority",
                            "scope",
                        ],
                        r,
                    )
                )
                if not isinstance(r, dict)
                else r
            )
            d["rule_config"] = rule_config_to_dict(
                rule_config_from_json(d["rule_type"], d["rule_config"])
            )
            rules.append(d)

        # Field access (with hierarchy)
        field_rows = await self.db.query(
            "WITH RECURSIVE role_tree AS ("
            "  SELECT group_id AS id FROM hyper_user_groups WHERE user_id = $1 "
            "  UNION ALL "
            "  SELECT g.parent_id FROM hyper_groups g "
            "  JOIN role_tree rt ON g.id = rt.id "
            "  WHERE g.parent_id IS NOT NULL"
            ") "
            "SELECT fp.model_name, fp.field_name, fp.access, "
            "CASE WHEN fp.user_id IS NOT NULL THEN 'direct' ELSE 'group' END AS source "
            "FROM hyper_field_permissions fp "
            "WHERE fp.user_id = $1 OR fp.group_id IN (SELECT id FROM role_tree)",
            user_id,
        )
        field_access = []
        for r in field_rows:
            d = (
                dict(zip(["model_name", "field_name", "access", "source"], r))
                if not isinstance(r, dict)
                else r
            )
            field_access.append(d)

        return {
            "user": user_info,
            "groups": group_list,
            "direct_permissions": direct_perms,
            "inherited_permissions": inherited_perms,
            "object_permissions": object_perms,
            "rules": rules,
            "field_access": field_access,
        }

    async def explain_permission_decision(
        self,
        user,
        perm: str,
        model_name: str,
        object_id: str | None = None,
        obj: PermissionObj | None = None,
        request: Request | None = None,
    ) -> dict[str, bool | list[dict[str, str | bool]]]:
        """Explain the full decision chain for a permission check.

        Returns {"allowed": bool, "steps": [{"check": str, "result": bool, "detail": str}]}
        Mirrors the has_perm_with_rules logic but records each decision point.
        """
        steps = []

        # Step 1: is_active
        is_active = user.is_active
        steps.append(
            {
                "check": "is_active",
                "result": is_active,
                "detail": "User is active" if is_active else "User is inactive",
            }
        )
        if not is_active:
            return {"allowed": False, "steps": steps}

        # Step 2: superuser
        is_su = _is_superuser(user)
        steps.append(
            {
                "check": "is_superuser",
                "result": is_su,
                "detail": "Superuser bypass" if is_su else "Not superuser",
            }
        )
        if is_su:
            return {"allowed": True, "steps": steps}

        # Step 3: model-level perm
        has_model = await self.has_perm(user, perm, model_name)
        # dynamic-attr: diagnostic read of the optional _perm_cache attr has_perm may have stashed on the user; absent when uncached
        perms = getattr(user, "_perm_cache", set())
        source = (
            f"{model_name}.{perm} in cache" if has_model else "not in user permissions"
        )
        steps.append({"check": "model_perm", "result": has_model, "detail": source})

        # Step 4: rules
        rules = await self._load_rules(user, perm, model_name)
        if not rules:
            steps.append(
                {
                    "check": "rules",
                    "result": True,
                    "detail": "No conditional rules defined",
                }
            )
            return {"allowed": has_model, "steps": steps}

        # Deny rules — mirror has_perm_with_rules: an inapplicable deny fails
        # closed (denies) rather than falling through to allow.
        deny_rules = [r for r in rules if r["is_deny"]]
        for rule in deny_rules:
            raw = await self._evaluate_rule(user, obj, request, rule)
            inapplicable = raw is INAPPLICABLE
            denied = inapplicable or bool(raw)
            if inapplicable:
                detail = f"Deny rule '{rule['rule_type']}' INAPPLICABLE (no context) — fail closed, access denied"
            elif denied:
                detail = f"Deny rule '{rule['rule_type']}' MATCHED — access denied"
            else:
                detail = f"Deny rule '{rule['rule_type']}' did not match"
            steps.append(
                {
                    "check": f"deny_rule:{rule['rule_type']}",
                    "result": denied,
                    "detail": detail,
                }
            )
            if denied:
                return {"allowed": False, "steps": steps}

        # Allow rules — only a definite match grants; INAPPLICABLE never does.
        allow_rules = [r for r in rules if not r["is_deny"]]
        if allow_rules:
            any_matched = False
            for rule in allow_rules:
                matched = await self._evaluate_rule(user, obj, request, rule) is True
                steps.append(
                    {
                        "check": f"allow_rule:{rule['rule_type']}",
                        "result": matched,
                        "detail": f"Allow rule '{rule['rule_type']}' {'MATCHED' if matched else 'did not match'}",
                    }
                )
                if matched:
                    any_matched = True
            if not any_matched:
                steps.append(
                    {
                        "check": "allow_rules_final",
                        "result": False,
                        "detail": "No allow rules matched",
                    }
                )
                return {"allowed": False, "steps": steps}
            return {"allowed": True, "steps": steps}

        return {"allowed": has_model, "steps": steps}

    # ── Policy import/export ──────────────────────────────────────────────

    async def export_policy(self) -> dict[str, int | list[dict[str, str | int | None]]]:
        """Export the complete RBAC policy as a JSON-serializable dict.

        Includes: groups (with hierarchy), permissions, user_groups,
        group_permissions, user_permissions, object_permissions,
        permission_rules, and field_permissions.

        Useful for backup, staging→production migration, disaster recovery.
        """
        # Groups
        groups = await self.db.query(
            "SELECT id, name, parent_id, priority FROM hyper_groups ORDER BY id"
        )
        group_cols = ["id", "name", "parent_id", "priority"]

        # Permissions
        perms = await self.db.query(
            "SELECT id, codename, name, model_name FROM hyper_permissions ORDER BY id"
        )
        perm_cols = ["id", "codename", "name", "model_name"]

        # User-group memberships
        user_groups = await self.db.query(
            "SELECT user_id, group_id FROM hyper_user_groups ORDER BY id"
        )
        ug_cols = ["user_id", "group_id"]

        # Group-permission assignments
        group_perms = await self.db.query(
            "SELECT group_id, permission_id FROM hyper_group_permissions ORDER BY id"
        )
        gp_cols = ["group_id", "permission_id"]

        # User-permission direct assignments
        user_perms = await self.db.query(
            "SELECT user_id, permission_id FROM hyper_user_permissions ORDER BY id"
        )
        up_cols = ["user_id", "permission_id"]

        # Object permissions
        obj_perms = await self.db.query(
            "SELECT user_id, group_id, permission_id, object_model, object_id "
            "FROM hyper_object_permissions ORDER BY id"
        )
        op_cols = ["user_id", "group_id", "permission_id", "object_model", "object_id"]

        # Permission rules
        rules = await self.db.query(
            "SELECT permission_id, rule_type, rule_config, group_id, user_id, priority, is_deny "
            "FROM hyper_permission_rules ORDER BY id"
        )
        rule_cols = [
            "permission_id",
            "rule_type",
            "rule_config",
            "group_id",
            "user_id",
            "priority",
            "is_deny",
        ]

        # Field permissions
        field_perms = await self.db.query(
            "SELECT model_name, field_name, group_id, user_id, access "
            "FROM hyper_field_permissions ORDER BY id"
        )
        fp_cols = ["model_name", "field_name", "group_id", "user_id", "access"]

        def to_dicts(rows, cols):
            result = []
            for r in rows:
                d = (
                    dict(zip(cols, r))
                    if not isinstance(r, dict)
                    else {c: r[c] for c in cols}
                )
                # Deserialize rule_config through typed system
                if "rule_config" in d:
                    rule_type = d.get("rule_type", "")
                    d["rule_config"] = rule_config_to_dict(
                        rule_config_from_json(rule_type, d["rule_config"])
                    )
                result.append(d)
            return result

        return {
            "version": 1,
            "exported_at": datetime.now(UTC).isoformat(),
            "groups": to_dicts(groups, group_cols),
            "permissions": to_dicts(perms, perm_cols),
            "user_groups": to_dicts(user_groups, ug_cols),
            "group_permissions": to_dicts(group_perms, gp_cols),
            "user_permissions": to_dicts(user_perms, up_cols),
            "object_permissions": to_dicts(obj_perms, op_cols),
            "permission_rules": to_dicts(rules, rule_cols),
            "field_permissions": to_dicts(field_perms, fp_cols),
        }

    async def import_policy(
        self, data: dict[str, object], *, clear_existing: bool = False
    ) -> dict[str, dict[str, int] | list[str]]:
        """Import RBAC policy from a dict (as produced by export_policy).

        By default merges with existing data (ON CONFLICT DO NOTHING).
        Set clear_existing=True to wipe and replace all RBAC data.

        Returns {"imported": {section: count}, "errors": [str]}
        """
        if data.get("version") != 1:
            return {
                "imported": {},
                "errors": [f"Unsupported policy version: {data.get('version')}"],
            }

        imported = {}
        errors = []

        if clear_existing:
            # Delete in reverse dependency order
            for table in [
                "hyper_field_permissions",
                "hyper_permission_rules",
                "hyper_object_permissions",
                "hyper_user_permissions",
                "hyper_group_permissions",
                "hyper_user_groups",
                "hyper_permissions",
                "hyper_groups",
            ]:
                await self.db.execute(f"DELETE FROM {table}")

        # Import order matters for FK constraints

        # 1. Groups (sorted by parent_id NULL first to handle hierarchy)
        groups = data.get("groups", [])
        # First pass: insert groups without parent_id
        count = 0
        for g in sorted(
            groups, key=lambda x: (x.get("parent_id") is not None, x.get("id", 0))
        ):
            try:
                if clear_existing:
                    await self.db.execute(
                        "INSERT INTO hyper_groups (id, name, parent_id, priority) "
                        "VALUES ($1, $2, $3, $4)",
                        g["id"],
                        g["name"],
                        g.get("parent_id"),
                        g.get("priority", 0),
                    )
                else:
                    await self.db.execute(
                        "INSERT INTO hyper_groups (id, name, parent_id, priority) "
                        "VALUES ($1, $2, $3, $4) "
                        "ON CONFLICT (id) DO UPDATE SET name = $2, parent_id = $3, priority = $4",
                        g["id"],
                        g["name"],
                        g.get("parent_id"),
                        g.get("priority", 0),
                    )
                count += 1
            # Bulk policy-import loop — a malformed/duplicate row (KeyError on a
            # missing field, or a DB constraint/IntegrityError) must be recorded
            # per-row and reported to the caller, not abort the whole import.
            # blind-except: admin bulk-import, per-row error collection, not auth.
            except Exception as e:
                errors.append(f"group {g.get('name')}: {e}")
        imported["groups"] = count

        # 2. Permissions
        count = 0
        for p in data.get("permissions", []):
            try:
                if clear_existing:
                    await self.db.execute(
                        "INSERT INTO hyper_permissions (id, codename, name, model_name) "
                        "VALUES ($1, $2, $3, $4)",
                        p["id"],
                        p["codename"],
                        p["name"],
                        p["model_name"],
                    )
                else:
                    await self.db.execute(
                        "INSERT INTO hyper_permissions (id, codename, name, model_name) "
                        "VALUES ($1, $2, $3, $4) "
                        "ON CONFLICT (codename, model_name) DO UPDATE SET name = $3",
                        p["id"],
                        p["codename"],
                        p["name"],
                        p["model_name"],
                    )
                count += 1
            # Bad/duplicate permission row is recorded per-row and reported to
            # the caller, not allowed to abort the import.
            # blind-except: admin bulk-import, per-row error collection, not auth.
            except Exception as e:
                errors.append(f"permission {p.get('codename')}: {e}")
        imported["permissions"] = count

        # 3. User-group memberships
        count = 0
        for ug in data.get("user_groups", []):
            try:
                await self.db.execute(
                    "INSERT INTO hyper_user_groups (user_id, group_id) "
                    "VALUES ($1, $2) ON CONFLICT DO NOTHING",
                    ug["user_id"],
                    ug["group_id"],
                )
                count += 1
            # Bad user_group row is recorded per-row and reported to the caller,
            # not allowed to abort the import.
            # blind-except: admin bulk-import, per-row error collection, not auth.
            except Exception as e:
                errors.append(f"user_group {ug}: {e}")
        imported["user_groups"] = count

        # 4. Group-permission assignments
        count = 0
        for gp in data.get("group_permissions", []):
            try:
                await self.db.execute(
                    "INSERT INTO hyper_group_permissions (group_id, permission_id) "
                    "VALUES ($1, $2) ON CONFLICT DO NOTHING",
                    gp["group_id"],
                    gp["permission_id"],
                )
                count += 1
            # Bad group_permission row is recorded per-row and reported to the
            # caller, not allowed to abort the import.
            # blind-except: admin bulk-import, per-row error collection, not auth.
            except Exception as e:
                errors.append(f"group_permission {gp}: {e}")
        imported["group_permissions"] = count

        # 5. User-permission direct assignments
        count = 0
        for up in data.get("user_permissions", []):
            try:
                await self.db.execute(
                    "INSERT INTO hyper_user_permissions (user_id, permission_id) "
                    "VALUES ($1, $2) ON CONFLICT DO NOTHING",
                    up["user_id"],
                    up["permission_id"],
                )
                count += 1
            # Bad user_permission row is recorded per-row and reported to the
            # caller, not allowed to abort the import.
            # blind-except: admin bulk-import, per-row error collection, not auth.
            except Exception as e:
                errors.append(f"user_permission {up}: {e}")
        imported["user_permissions"] = count

        # 6. Object permissions
        count = 0
        for op in data.get("object_permissions", []):
            try:
                await self.db.execute(
                    "INSERT INTO hyper_object_permissions (user_id, group_id, permission_id, object_model, object_id) "
                    "VALUES ($1, $2, $3, $4, $5) ON CONFLICT DO NOTHING",
                    op.get("user_id"),
                    op.get("group_id"),
                    op["permission_id"],
                    op["object_model"],
                    op["object_id"],
                )
                count += 1
            # Bad object_permission row is recorded per-row and reported to the
            # caller, not allowed to abort the import.
            # blind-except: admin bulk-import, per-row error collection, not auth.
            except Exception as e:
                errors.append(f"object_permission {op}: {e}")
        imported["object_permissions"] = count

        # 7. Permission rules
        count = 0
        for rule in data.get("permission_rules", []):
            try:
                raw_config = rule.get("rule_config", {})
                rule_type = rule.get("rule_type", "")
                typed_config = rule_config_from_json(rule_type, raw_config)
                config_json = rule_config_to_json(typed_config)
                await self.db.execute(
                    "INSERT INTO hyper_permission_rules (permission_id, rule_type, rule_config, group_id, user_id, priority, is_deny) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7)",
                    rule["permission_id"],
                    rule["rule_type"],
                    config_json,
                    rule.get("group_id"),
                    rule.get("user_id"),
                    rule.get("priority", 0),
                    rule.get("is_deny", False),
                )
                count += 1
            # Bad rule row (invalid rule_config, missing field, or DB error) is
            # recorded per-row and reported to the caller, not allowed to abort
            # the import.
            # blind-except: admin bulk-import, per-row error collection, not auth.
            except Exception as e:
                errors.append(f"rule {rule}: {e}")
        imported["permission_rules"] = count

        # 8. Field permissions
        count = 0
        for fp in data.get("field_permissions", []):
            try:
                await self.db.execute(
                    "INSERT INTO hyper_field_permissions (model_name, field_name, group_id, user_id, access) "
                    "VALUES ($1, $2, $3, $4, $5) ON CONFLICT DO NOTHING",
                    fp["model_name"],
                    fp["field_name"],
                    fp.get("group_id"),
                    fp.get("user_id"),
                    fp.get("access", "hidden"),
                )
                count += 1
            # Bad field_permission row is recorded per-row and reported to the
            # caller, not allowed to abort the import.
            # blind-except: admin bulk-import, per-row error collection, not auth.
            except Exception as e:
                errors.append(f"field_permission {fp}: {e}")
        imported["field_permissions"] = count

        # Reset sequences to max id
        for table, seq in [
            ("hyper_groups", "hyper_groups_id_seq"),
            ("hyper_permissions", "hyper_permissions_id_seq"),
        ]:
            with contextlib.suppress(Exception):
                await self.db.execute(
                    f"SELECT setval('{seq}', COALESCE((SELECT MAX(id) FROM {table}), 1))"
                )

        await self._audit.log(
            "import_policy",
            "policy",
            "",
            {
                "imported": imported,
                "clear_existing": clear_existing,
                "error_count": len(errors),
            },
        )

        return {"imported": imported, "errors": errors}

    # ── Tenant-scoped permissions ─────────────────────────────────────────

    async def grant_tenant_perm(
        self,
        user_id: int,
        codename: str,
        model_name: str,
        tenant_id: int,
    ) -> None:
        """Grant a permission to a user within a specific tenant.

        Uses INSERT...SELECT pattern matching grant_user_perm.
        """
        await self.db.execute(
            "INSERT INTO hyper_user_permissions (user_id, permission_id, tenant_id) "
            "SELECT $1, p.id, $3 FROM hyper_permissions p "
            "WHERE p.codename = $2 AND p.model_name = $4 "
            "ON CONFLICT DO NOTHING",
            user_id,
            codename,
            tenant_id,
            model_name,
        )
        await self._audit.log(
            "grant_tenant_perm",
            "user",
            str(user_id),
            {"codename": codename, "model": model_name, "tenant_id": tenant_id},
        )

    async def revoke_tenant_perm(
        self,
        user_id: int,
        codename: str,
        model_name: str,
        tenant_id: int,
    ) -> None:
        """Revoke a tenant-scoped permission from a user."""
        await self.db.execute(
            "DELETE FROM hyper_user_permissions "
            "WHERE user_id = $1 AND tenant_id = $2 "
            "AND permission_id = (SELECT id FROM hyper_permissions WHERE codename = $3 AND model_name = $4)",
            user_id,
            tenant_id,
            codename,
            model_name,
        )
        await self._audit.log(
            "revoke_tenant_perm",
            "user",
            str(user_id),
            {"codename": codename, "model": model_name, "tenant_id": tenant_id},
        )

    async def has_tenant_perm(
        self,
        user,
        perm: str,
        model_name: str | None = None,
        tenant_id: int | None = None,
    ) -> bool:
        """Check if a user has a permission within a specific tenant.

        If tenant_id is None, uses the current tenant context.
        Returns True if the user has the permission globally OR in the specified tenant.
        """
        if not user.is_active:
            return False
        if _is_superuser(user):
            return True

        if tenant_id is not None:
            # Explicit tenant_id — set context for the duration of this check
            with tenant_context(tenant_id):
                return await self.has_perm(user, perm, model_name)

        # No explicit tenant_id — use current context (or global)
        return await self.has_perm(user, perm, model_name)


# ── Built-in rule evaluators ─────────────────────────────────────────────────
#
# Evaluators are TRISTATE: they return True (rule matches), False (rule
# definitely does not match), or INAPPLICABLE (the rule cannot be evaluated
# because required context — obj or request — is missing). Conflating
# "inapplicable" with False is a security bug for DENY rules: a deny rule that
# merely cannot fire would fall through to allow. has_perm_with_rules treats an
# inapplicable DENY as fail-closed (deny); an inapplicable ALLOW never grants.
INAPPLICABLE = object()


def _eval_is_owner(user, obj: PermissionObj | None, config: IsOwnerConfig):
    """Check if the user owns the object (obj.owner_field == user.id).

    Returns INAPPLICABLE when there is no object to test ownership against.
    """
    if obj is None:
        return INAPPLICABLE
    owner_field = config.owner_field
    # dynamic-attr: owner_field is a rule-config-driven field name read off an arbitrary permission-checked Model (dict objects fall through below)
    obj_owner = getattr(obj, owner_field, None)
    if obj_owner is None and isinstance(obj, dict):
        obj_owner = obj.get(owner_field)
    user_id = _user_pk(user)
    return obj_owner is not None and obj_owner == user_id


def _eval_time_window(config: TimeWindowConfig) -> bool:
    """Check if current time is within the allowed window."""
    try:
        tz = ZoneInfo(config.timezone)
    except KeyError:
        tz = UTC
    now = datetime.now(tz)
    h, m = map(int, config.start.split(":"))
    start_minutes = h * 60 + m
    h, m = map(int, config.end.split(":"))
    end_minutes = h * 60 + m
    current_minutes = now.hour * 60 + now.minute
    if start_minutes <= end_minutes:
        return start_minutes <= current_minutes <= end_minutes
    # Wraps midnight (e.g., 22:00 → 06:00)
    return current_minutes >= start_minutes or current_minutes <= end_minutes


def _eval_ip_range(request: Request | None, config: IpRangeConfig):
    """Check if request IP is in allowed CIDR ranges.

    Returns INAPPLICABLE when there is no request (or no client IP) to test —
    "cannot evaluate", distinct from "IP not in range".
    """
    if request is None:
        return INAPPLICABLE
    client_ip = request.client_ip
    if not client_ip:
        return INAPPLICABLE
    try:
        addr = ipaddress.ip_address(client_ip)
        return any(addr in ipaddress.ip_network(r, strict=False) for r in config.ranges)
    except ValueError:
        return False


def _eval_field_match(obj: PermissionObj | None, config: FieldMatchConfig):
    """Check if object field matches allowed values.

    Returns INAPPLICABLE when there is no object to read the field from.
    """
    if obj is None:
        return INAPPLICABLE
    # dynamic-attr: field_name is a rule-config-driven attribute name read off an arbitrary permission-checked Model (dict objects fall through below)
    val = getattr(obj, config.field_name, None)
    if val is None and isinstance(obj, dict):
        val = obj.get(config.field_name)
    return val in config.values


async def _eval_custom(
    user, obj: PermissionObj | None, request: Request | None, config: CustomRuleConfig
) -> bool:
    """Call a custom evaluator function by module path. Supports sync and async."""
    if not config.module or not config.function:
        return False
    try:
        mod = importlib.import_module(config.module)
        # dynamic-attr: plugin dispatch — resolve a custom evaluator function by its config-supplied name from a dynamically imported module
        func = getattr(mod, config.function)
        if inspect.iscoroutinefunction(func):
            return await func(user, obj, request, config)
        return func(user, obj, request, config)
    except ImportError, AttributeError, TypeError:
        return False
