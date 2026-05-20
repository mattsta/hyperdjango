"""
HyperGuard requirement factories — Require.authenticated(), Require.resource(), etc.

Each factory returns a frozen GuardRequirement with an async evaluate_fn.
Requirements are composed into a guard chain via @guard(Require.a(), Require.b(), ...).
"""

from collections.abc import Callable, Coroutine
from typing import Any

from hyperdjango.auth.user import SessionUser
from hyperdjango.guard.types import (
    _REDIRECT_URL_KEY,
    DenyReason,
    GuardContext,
    GuardDenial,
    GuardRequirement,
    RequirementKind,
)
from hyperdjango.logging import logger
from hyperdjango.timeline import get_timeline

_TIMELINE_CACHE_KEY = "_timeline_active_statuses"

# Tristate results for the timeline lookup. Distinguishing "no DB configured"
# (UNAVAILABLE) from "the DB errored" (ERROR) is a security requirement: a real
# DB error must FAIL CLOSED (deny) for ban/mute checks, while an absent timeline
# DB is a benign not-configured state that ban/mute allow through. Collapsing
# both to None (as the old code did) let a genuine DB error read as "unavailable
# → allow", so a banned user sailed past not_banned during a DB outage.
_TIMELINE_UNAVAILABLE = object()  # no database configured / table not provisioned
_TIMELINE_ERROR = object()  # real DB error / bug → callers must fail closed
_NOT_CACHED = object()  # sentinel distinguishing "absent" from a cached result


def _timeline_table_missing(exc: BaseException) -> bool:
    """Whether a timeline query failed ONLY because its table isn't provisioned.

    A database that is reachable but has never had the timeline schema
    installed holds no status data at all — there is nothing a fail-closed
    denial would protect, so callers treat it exactly like "no database
    configured". Every OTHER exception stays a real error (fail closed).
    """
    # Lazy import: pgzig_connection pulls in psycopg; guard evaluation must
    # not pay that import cost on the hot path — only on the error path.
    from hyperdjango.db.pgzig_connection import is_undefined_table

    return is_undefined_table(exc)


async def _get_cached_active_statuses(user_id: int, ctx: GuardContext):
    """Get active timeline statuses for a user, cached per guard chain.

    Returns one of:
      - ``set[str]`` — the active status strings (timeline answered),
      - ``_TIMELINE_UNAVAILABLE`` — no database configured or the timeline
        table isn't provisioned (benign),
      - ``_TIMELINE_ERROR`` — a genuine DB error/bug (callers fail closed).

    Caches in ctx.metadata so multiple guards in the same chain share ONE query.
    """
    cached = ctx.metadata.get(_TIMELINE_CACHE_KEY, _NOT_CACHED)
    if cached is not _NOT_CACHED:
        return cached

    try:
        tl = get_timeline()
        statuses = await tl.active_statuses("user", user_id)
        ctx.metadata[_TIMELINE_CACHE_KEY] = statuses
        return statuses
    except RuntimeError:
        # No database configured — unavailable (distinct from an error).
        ctx.metadata[_TIMELINE_CACHE_KEY] = _TIMELINE_UNAVAILABLE
        return _TIMELINE_UNAVAILABLE
    # A real DB error/bug is converted to the _TIMELINE_ERROR sentinel (distinct
    # from the RuntimeError "unavailable" case above), which makes every caller
    # fail CLOSED. Logged, not silently swallowed.
    # blind-except: security decision — DB error → ERROR sentinel, callers deny.
    except Exception as exc:
        if _timeline_table_missing(exc):
            # hyper_status_events not provisioned — the timeline holds no data
            # anywhere, so this is the SAME benign not-configured state as "no
            # database": nothing to fail closed over. A reachable DB whose
            # schema lacks the table (fresh install, tests) must not deny.
            ctx.metadata[_TIMELINE_CACHE_KEY] = _TIMELINE_UNAVAILABLE
            return _TIMELINE_UNAVAILABLE
        # Real DB error / programming bug — signal ERROR so callers fail closed.
        logger.error("[GUARD] Timeline lookup failed for user {uid}", uid=user_id)
        ctx.metadata[_TIMELINE_CACHE_KEY] = _TIMELINE_ERROR
        return _TIMELINE_ERROR


class Require:
    """Factory for guard requirements.

    Usage:
        @guard(
            Require.authenticated(),
            Require.not_banned(),
            Require.resource("forum", resolver=resolve_forum, from_path="forum_name"),
            Require.check("karma", fn=check_karma),
        )
        async def my_route(request, forum_name: str):
            forum = request.guard.forum

    Requirement ordering matters — requirements are evaluated in declaration
    order and short-circuit on first failure. Place cheap preconditions
    (authenticated, not_banned) before expensive resource resolvers.
    """

    @staticmethod
    def authenticated(*, redirect_url: str | None = None) -> GuardRequirement:
        """Require an authenticated user (session dict with 'id' key).

        Args:
            redirect_url: If set, redirect unauthenticated users instead of 401.
        """

        async def _evaluate(request: Any, ctx: GuardContext) -> GuardDenial | None:
            user = request.user
            if user is not None and user.is_authenticated:
                return None
            if redirect_url is not None:
                ctx.metadata[_REDIRECT_URL_KEY] = redirect_url
            return GuardDenial(DenyReason.NOT_AUTHENTICATED, "Authentication required")

        return GuardRequirement(
            kind=RequirementKind.PRECONDITION,
            name="authenticated",
            evaluate_fn=_evaluate,
        )

    @staticmethod
    def staff() -> GuardRequirement:
        """Require staff access.

        Checks (in order):
        1. RBAC groups for "staff" (via ``user.in_group()``)
        2. Timeline "staff" status (cached — shared across guard chain)
        """

        async def _evaluate(request: Any, ctx: GuardContext) -> GuardDenial | None:
            user = request.user
            if user is None or not user.is_authenticated:
                return GuardDenial(
                    DenyReason.NOT_AUTHENTICATED, "Authentication required"
                )

            if user.in_group("staff"):
                return None

            # Timeline fallback — only a real status set can GRANT staff.
            # UNAVAILABLE/ERROR simply don't grant (deny by default below).
            user_id = user.id
            if user_id is not None:
                statuses = await _get_cached_active_statuses(user_id, ctx)
                if isinstance(statuses, set) and "staff" in statuses:
                    return None

            return GuardDenial(DenyReason.FORBIDDEN, "Staff access required")

        return GuardRequirement(
            kind=RequirementKind.PRECONDITION,
            name="staff",
            evaluate_fn=_evaluate,
        )

    @staticmethod
    def group(group_name: str, *, deny_message: str = "") -> GuardRequirement:
        """Require the user to be a member of a named RBAC group.

        Uses ``user.in_group()`` — O(1) frozenset membership check.
        No DB query — groups are resolved at authentication time.
        """

        async def _evaluate(request: Any, ctx: GuardContext) -> GuardDenial | None:
            user = request.user
            if user is None or not user.is_authenticated:
                return GuardDenial(
                    DenyReason.NOT_AUTHENTICATED, "Authentication required"
                )
            if user.in_group(group_name):
                return None
            msg = deny_message or f"Requires {group_name} role"
            return GuardDenial(DenyReason.FORBIDDEN, msg)

        return GuardRequirement(
            kind=RequirementKind.PRECONDITION,
            name=f"group:{group_name}",
            evaluate_fn=_evaluate,
        )

    @staticmethod
    def role(role_name: str, *, deny_message: str = "") -> GuardRequirement:
        """Require the user to have a named RBAC role (alias for ``group()``).

        Semantically identical to ``Require.group(role_name)`` — roles ARE
        groups in the HyperDjango RBAC model. This alias reads more naturally
        in role-based contexts: ``@guard(Require.role("admin"))``.
        """
        return Require.group(role_name, deny_message=deny_message)

    @staticmethod
    def permission(codename: str, *, deny_message: str = "") -> GuardRequirement:
        """Require the user to have a specific RBAC permission codename.

        Uses ``user.has_perm()`` — checks permissions frozenset and
        superuser group bypass. O(1) lookups, no DB query.
        """

        async def _evaluate(request: Any, ctx: GuardContext) -> GuardDenial | None:
            user = request.user
            if user is None or not user.is_authenticated:
                return GuardDenial(
                    DenyReason.NOT_AUTHENTICATED, "Authentication required"
                )
            if user.has_perm(codename):
                return None
            msg = deny_message or f"Requires '{codename}' permission"
            return GuardDenial(DenyReason.FORBIDDEN, msg)

        return GuardRequirement(
            kind=RequirementKind.PRECONDITION,
            name=f"permission:{codename}",
            evaluate_fn=_evaluate,
        )

    @staticmethod
    def field_access(
        field_name: str, model_name: str, *, level: str = "writable"
    ) -> GuardRequirement:
        """Require the user has a specific field-level access on a model.

        Checks ``user.field_access`` (populated at login from RBAC
        FieldPermission table). The field must have at least the requested
        ``level``: "writable" (default), "readonly", or any non-"hidden".

        No DB query — field permissions are cached in the session.
        """
        _LEVEL_RANK = {"hidden": 0, "readonly": 1, "writable": 2}
        required_rank = _LEVEL_RANK.get(level, 2)

        async def _evaluate(request: Any, ctx: GuardContext) -> GuardDenial | None:
            user = request.user
            if user is None or not user.is_authenticated:
                return GuardDenial(
                    DenyReason.NOT_AUTHENTICATED, "Authentication required"
                )
            # Superuser bypasses all field restrictions
            if user.in_group("superuser"):
                return None
            field_access = user.get("field_access") or {}
            model_fields = field_access.get(model_name) or {}
            # Fail CLOSED: an ABSENT field or an UNKNOWN/typo'd level defaults to
            # the most restrictive access ("hidden", rank 0), never "writable".
            # The old "writable" default meant a missing field-permission row —
            # or a single typo in the stored access level — silently granted full
            # access, and an empty/partial field_access map (e.g. from a login
            # RBAC hiccup) opened every field.
            actual_level = model_fields.get(field_name, "hidden")
            actual_rank = _LEVEL_RANK.get(actual_level, 0)
            if actual_rank >= required_rank:
                return None
            return GuardDenial(
                DenyReason.FORBIDDEN,
                f"Insufficient access to {model_name}.{field_name} (need {level}, have {actual_level})",
            )

        return GuardRequirement(
            kind=RequirementKind.PRECONDITION,
            name=f"field_access:{model_name}.{field_name}:{level}",
            evaluate_fn=_evaluate,
        )

    @staticmethod
    def not_banned() -> GuardRequirement:
        """Require the authenticated user is not banned.

        Checks session dict and timeline (cached) — EITHER source denies.
        Fail-closed on DB errors.
        """

        async def _evaluate(request: Any, ctx: GuardContext) -> GuardDenial | None:
            user = request.user
            if user is None or not user.is_authenticated:
                return GuardDenial(
                    DenyReason.NOT_AUTHENTICATED, "Authentication required"
                )

            if user.get("is_banned", False):
                return GuardDenial(DenyReason.FORBIDDEN, "Your account is suspended")

            user_id = user.id
            if user_id is not None:
                statuses = await _get_cached_active_statuses(user_id, ctx)
                if statuses is _TIMELINE_ERROR:
                    # Genuine DB error — FAIL CLOSED (deny), per the docstring.
                    # Never let a ban check silently pass during a DB outage.
                    return GuardDenial(
                        DenyReason.FORBIDDEN, "Account status check unavailable"
                    )
                if statuses is _TIMELINE_UNAVAILABLE:
                    # No timeline DB configured AND session flag was False — allow
                    return None
                if "banned" in statuses:
                    return GuardDenial(
                        DenyReason.FORBIDDEN, "Your account is suspended"
                    )

            return None

        return GuardRequirement(
            kind=RequirementKind.PRECONDITION,
            name="not_banned",
            evaluate_fn=_evaluate,
        )

    @staticmethod
    def not_muted() -> GuardRequirement:
        """Require the authenticated user is not muted.

        Checks session dict and timeline (cached) — EITHER source denies.
        Fail-closed on DB errors.
        """

        async def _evaluate(request: Any, ctx: GuardContext) -> GuardDenial | None:
            user = request.user
            if user is None or not user.is_authenticated:
                return GuardDenial(
                    DenyReason.NOT_AUTHENTICATED, "Authentication required"
                )

            if user.get("is_muted", False):
                return GuardDenial(DenyReason.FORBIDDEN, "Your account has been muted")

            user_id = user.id
            if user_id is not None:
                statuses = await _get_cached_active_statuses(user_id, ctx)
                if statuses is _TIMELINE_ERROR:
                    # Genuine DB error — FAIL CLOSED (deny), per the docstring.
                    return GuardDenial(
                        DenyReason.FORBIDDEN, "Account status check unavailable"
                    )
                if statuses is _TIMELINE_UNAVAILABLE:
                    return None
                if "muted" in statuses:
                    return GuardDenial(
                        DenyReason.FORBIDDEN, "Your account has been muted"
                    )

            return None

        return GuardRequirement(
            kind=RequirementKind.PRECONDITION,
            name="not_muted",
            evaluate_fn=_evaluate,
        )

    @staticmethod
    def no_active_status(
        category: str,
        status: str,
        *,
        deny_message: str = "",
        entity_type: str = "user",
        fallback_flag: str = "",
    ) -> GuardRequirement:
        """Deny if the authenticated user has an active timeline status.

        Queries the StatusTimeline for the user's entity. Falls back to
        checking ``fallback_flag`` on the session dict ONLY when the
        timeline table doesn't exist yet (ImportError/DB setup).

        Fail-closed: if the timeline query errors for any other reason
        (DB down, programming bug), access is DENIED with logging.
        """

        async def _evaluate(request: Any, ctx: GuardContext) -> GuardDenial | None:
            user = request.user
            if user is None or not user.is_authenticated:
                return GuardDenial(
                    DenyReason.NOT_AUTHENTICATED, "Authentication required"
                )

            user_id = user.id
            if user_id is None:
                return GuardDenial(
                    DenyReason.NOT_AUTHENTICATED, "Authentication required"
                )

            msg = deny_message or f"Access denied: {status}"

            # Try timeline
            try:
                tl = get_timeline()
                if await tl.is_active(entity_type, user_id, status):
                    return GuardDenial(DenyReason.FORBIDDEN, msg)
                return None  # Timeline says no active status — allow
            except RuntimeError:
                # No database configured yet — fall through to session flag
                pass
            # A real DB error is converted to a FORBIDDEN denial (fail CLOSED).
            # RuntimeError "no db configured" and a not-yet-provisioned
            # timeline table both fall through to the session flag instead.
            # blind-except: security decision — DB error fails CLOSED (deny).
            except Exception as exc:
                if not _timeline_table_missing(exc):
                    # Real DB error or bug — fail closed (deny), log category
                    logger.error(
                        "[GUARD] Timeline lookup failed for {cat}, denying access",
                        cat=f"{category}/{status}",
                    )
                    return GuardDenial(DenyReason.FORBIDDEN, msg)

            # Fallback: session flag (only when timeline unavailable)
            if fallback_flag and user.get(fallback_flag, False):
                return GuardDenial(DenyReason.FORBIDDEN, msg)

            return None

        return GuardRequirement(
            kind=RequirementKind.PRECONDITION,
            name=f"no_active_status:{category}/{status}",
            evaluate_fn=_evaluate,
        )

    @staticmethod
    def has_active_status(
        category: str,
        status: str,
        *,
        deny_message: str = "",
        entity_type: str = "user",
        fallback_flag: str = "",
    ) -> GuardRequirement:
        """Require the authenticated user HAS an active timeline status.

        Falls back to session dict flag ONLY when timeline is unavailable.
        If timeline is available and says "no", fallback is NOT checked —
        timeline is the source of truth.
        """

        async def _evaluate(request: Any, ctx: GuardContext) -> GuardDenial | None:
            user = request.user
            if user is None or not user.is_authenticated:
                return GuardDenial(
                    DenyReason.NOT_AUTHENTICATED, "Authentication required"
                )

            user_id = user.id
            if user_id is None:
                return GuardDenial(
                    DenyReason.NOT_AUTHENTICATED, "Authentication required"
                )

            msg = deny_message or f"Required status: {status}"
            timeline_available = False

            # Try timeline
            try:
                tl = get_timeline()
                if await tl.is_active(entity_type, user_id, status):
                    return None  # Has status — allow
                timeline_available = True  # Timeline answered — it's authoritative
            except RuntimeError:
                # No database configured yet — fall through to session flag
                pass
            # A real DB error is converted to a FORBIDDEN denial (fail CLOSED).
            # RuntimeError "no db configured" and a not-yet-provisioned
            # timeline table both fall through to the session flag instead.
            # blind-except: security decision — DB error fails CLOSED (deny).
            except Exception as exc:
                if not _timeline_table_missing(exc):
                    # Real DB error — fail closed (deny)
                    logger.error(
                        "[GUARD] Timeline lookup failed for {cat}, denying access",
                        cat=f"{category}/{status}",
                    )
                    return GuardDenial(DenyReason.FORBIDDEN, msg)

            # If timeline answered "no", that's final — don't check fallback
            if timeline_available:
                return GuardDenial(DenyReason.FORBIDDEN, msg)

            # Fallback: session flag (only when timeline unavailable)
            if fallback_flag and user.get(fallback_flag, False):
                return None  # Has flag — allow

            return GuardDenial(DenyReason.FORBIDDEN, msg)

        return GuardRequirement(
            kind=RequirementKind.PRECONDITION,
            name=f"has_active_status:{category}/{status}",
            evaluate_fn=_evaluate,
        )

    @staticmethod
    def api_key() -> GuardRequirement:
        """Require a valid API key (set by APIKeyAuth middleware)."""

        async def _evaluate(request: Any, ctx: GuardContext) -> GuardDenial | None:
            if request.api_key_valid:
                return None
            return GuardDenial(
                DenyReason.NOT_AUTHENTICATED,
                "Valid API key required",
                status_code=401,
            )

        return GuardRequirement(
            kind=RequirementKind.PRECONDITION,
            name="api_key",
            evaluate_fn=_evaluate,
        )

    @staticmethod
    def superuser() -> GuardRequirement:
        """Require the authenticated user is a superuser.

        Checks session ``groups`` list for "superuser" (RBAC groups —
        populated at login via ``build_session_data()``).
        """

        async def _evaluate(request: Any, ctx: GuardContext) -> GuardDenial | None:
            user = request.user
            if user is None or not user.is_authenticated:
                return GuardDenial(
                    DenyReason.NOT_AUTHENTICATED, "Authentication required"
                )
            if user.in_group("superuser"):
                return None
            return GuardDenial(DenyReason.FORBIDDEN, "Superuser access required")

        return GuardRequirement(
            kind=RequirementKind.PRECONDITION,
            name="superuser",
            evaluate_fn=_evaluate,
        )

    @staticmethod
    def resource(
        key: str,
        *,
        resolver: Callable[..., Coroutine[Any, Any, object | None]],
        from_path: str | None = None,
        deny_message: str = "",
    ) -> GuardRequirement:
        """Resolve a named resource and store it in guard context.

        Args:
            key: Name for the resolved resource (e.g., "forum", "post").
                 Accessible as request.guard.<key> after evaluation.
            resolver: Async callable(request, guard_context, path_val) -> resource | None.
                      Return None to trigger 404. Raise HTTPException for custom errors.
                      The resolver receives the full request and accumulated guard context,
                      so it can access previously-resolved resources.
            from_path: If set, extract this path parameter and pass it as the third
                       positional arg to the resolver. Convenience for single-param lookups.
            deny_message: Custom 404 message when resolver returns None.
        """

        async def _evaluate(request: Any, ctx: GuardContext) -> GuardDenial | None:
            if from_path is not None:
                path_val = request.path_params.get(from_path)
                if path_val is None:
                    return GuardDenial(
                        DenyReason.RESOURCE_NOT_FOUND,
                        deny_message or f"{key.capitalize()} not found",
                    )
                result = await resolver(request, ctx, path_val)
            else:
                result = await resolver(request, ctx)
            if result is None:
                return GuardDenial(
                    DenyReason.RESOURCE_NOT_FOUND,
                    deny_message or f"{key.capitalize()} not found",
                )
            ctx.resources[key] = result
            return None

        return GuardRequirement(
            kind=RequirementKind.RESOURCE,
            name=f"resource:{key}",
            evaluate_fn=_evaluate,
            resource_key=key,
        )

    @staticmethod
    def check(
        name: str,
        *,
        fn: Callable[..., Coroutine[Any, Any, GuardDenial | None]],
    ) -> GuardRequirement:
        """Custom async check — full control over evaluation.

        Args:
            name: Human-readable requirement name for logging.
            fn: Async callable(request, guard_context) -> GuardDenial | None.
                Return None to pass, return GuardDenial to deny.
        """
        return GuardRequirement(
            kind=RequirementKind.CUSTOM,
            name=name,
            evaluate_fn=fn,
        )

    @staticmethod
    def any_of(*requirements: GuardRequirement) -> GuardRequirement:
        """OR composition — pass if ANY requirement passes.

        Short-circuits on first pass. If all fail, returns the last denial.
        Each alternative gets a snapshot of the context to prevent partial
        side effects from failing alternatives.
        """

        async def _evaluate(request: Any, ctx: GuardContext) -> GuardDenial | None:
            last_denial: GuardDenial | None = None
            for req in requirements:
                # Snapshot resources AND metadata before this alternative runs.
                # If it fails, roll back to prevent partial side effects.
                # NOTE: shallow copy — requirement evaluators must NOT mutate
                # objects already in ctx.resources from prior requirements.
                res_snapshot = dict(ctx.resources)
                meta_snapshot = dict(ctx.metadata)
                result = await req.evaluate_fn(request, ctx)
                if result is None:
                    return None  # Passed — keep any changes it made
                # Failed — roll back both resources and metadata
                ctx.resources.clear()
                ctx.resources.update(res_snapshot)
                ctx.metadata.clear()
                ctx.metadata.update(meta_snapshot)
                last_denial = result
            return last_denial  # All failed

        names = " | ".join(r.name for r in requirements)
        return GuardRequirement(
            kind=RequirementKind.CUSTOM,
            name=f"any_of({names})",
            evaluate_fn=_evaluate,
        )

    @staticmethod
    def policy(
        resource_action: str,
        *,
        registry: object,  # PolicyRegistry (typed as object to avoid circular import)
        resource_dict_fn: Callable[..., dict[str, object]] | None = None,
    ) -> GuardRequirement:
        """Evaluate a compiled policy from a PolicyRegistry.

        Bridges Phase 1 (@guard decorator) with Phase 3 (policy files).
        Extracts user dict from request.user and resource dict from
        previously-resolved guard context resources.

        Args:
            resource_action: "Resource.action" string (e.g., "Forum.write_post").
            registry: PolicyRegistry instance with loaded policies.
            resource_dict_fn: Optional callable(request, ctx) -> dict that provides
                              the resource dict for evaluation. If None, an empty
                              dict is used (useful for user-only checks like
                              "allow read where { user.is_staff = true }").

        Usage:
            @guard(
                Require.authenticated(),
                Require.resource("forum", resolver=resolve_forum, from_path="name"),
                Require.policy("Forum.write_post", registry=registry,
                               resource_dict_fn=lambda r, ctx: {
                                   "is_archived": ctx.forum.is_archived,
                                   "is_locked": ctx.forum.is_locked,
                                   "is_public": ctx.forum.is_public,
                               }),
            )
        """
        parts = resource_action.split(".", 1)
        if len(parts) != 2:
            msg = f"Invalid resource_action {resource_action!r} — expected 'Resource.action'"
            raise ValueError(msg)
        resource_name, action = parts

        async def _evaluate(request: Any, ctx: GuardContext) -> GuardDenial | None:
            # Zig bytecode evaluator requires a real dict for PyDict_GetItemString FFI
            user = request.user
            if user is not None and user.is_authenticated:
                user_dict = user._data if isinstance(user, SessionUser) else {}
            else:
                user_dict = {}
            if resource_dict_fn is not None:
                resource_dict = resource_dict_fn(request, ctx)
            else:
                resource_dict = {}

            try:
                allowed = registry.evaluate(
                    resource_name, action, user_dict, resource_dict
                )
            # Any policy-evaluation error (Zig bytecode/FFI fault, bad policy) is
            # converted to a FORBIDDEN denial (fail CLOSED) and logged with
            # traceback so misconfigurations are visible.
            # blind-except: security decision — eval error fails CLOSED (deny).
            except Exception:
                # Fail closed: evaluation error = deny. Log so misconfigurations
                # are visible rather than silently swallowed.
                logger.exception(
                    "[GUARD] Policy evaluation error for {ra}",
                    ra=resource_action,
                )
                return GuardDenial(
                    DenyReason.FORBIDDEN, f"Policy evaluation error: {resource_action}"
                )
            if allowed:
                return None
            return GuardDenial(
                DenyReason.FORBIDDEN, f"Policy denied: {resource_action}"
            )

        return GuardRequirement(
            kind=RequirementKind.CUSTOM,
            name=f"policy:{resource_action}",
            evaluate_fn=_evaluate,
        )
