"""
HyperGuard type definitions — frozen dataclasses for compiled permission specs.

All types are immutable (frozen dataclass). GuardSpec is the compiled output
of a requirement chain — built once at route registration, evaluated per-request.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum


class RequirementKind(Enum):
    """Classification of guard requirements.

    PRECONDITION: Checks request state (authenticated, staff, rate limit).
                  No resource resolution needed. Fast, no DB queries.
    RESOURCE:     Resolves a named resource from path params + DB lookup,
                  then checks access (intent, ownership, membership).
    CUSTOM:       User-provided async callable for app-specific logic.
    """

    PRECONDITION = "precondition"
    RESOURCE = "resource"
    CUSTOM = "custom"


class DenyReason(Enum):
    """Standardized denial reasons for consistent error responses."""

    NOT_AUTHENTICATED = "not_authenticated"
    NOT_STAFF = "not_staff"
    RATE_LIMITED = "rate_limited"
    RESOURCE_NOT_FOUND = "resource_not_found"
    FORBIDDEN = "forbidden"
    CUSTOM = "custom"


_DENY_STATUS: dict[DenyReason, int] = {
    DenyReason.NOT_AUTHENTICATED: 401,
    DenyReason.NOT_STAFF: 403,
    DenyReason.RATE_LIMITED: 429,
    DenyReason.RESOURCE_NOT_FOUND: 404,
    DenyReason.FORBIDDEN: 403,
    DenyReason.CUSTOM: 403,
}


@dataclass(frozen=True)
class GuardDenial:
    """Result of a failed requirement evaluation."""

    reason: DenyReason
    message: str
    status_code: int = 0  # 0 = auto from reason
    retry_after: int | None = None  # seconds; emitted as Retry-After on 429

    @property
    def effective_status(self) -> int:
        if self.status_code:
            return self.status_code
        return _DENY_STATUS.get(self.reason, 403)


# Type alias for requirement evaluator functions.
# async (request, GuardContext) -> GuardDenial | None
EvaluateFn = Callable[..., Awaitable[GuardDenial | None]]


@dataclass(frozen=True)
class GuardRequirement:
    """Single requirement in a guard chain.

    Built by Require.* factory methods. Immutable after creation.
    The evaluator calls `evaluate_fn(request, context)` at runtime.
    """

    kind: RequirementKind
    name: str  # Human-readable for logging/errors: "authenticated", "forum.write_post"
    evaluate_fn: EvaluateFn
    # For resource requirements: which key in GuardContext.resources to store the result
    resource_key: str | None = None


@dataclass(frozen=True)
class GuardSpec:
    """Compiled guard specification for a route.

    Built once at decoration time from a sequence of Require.* calls.
    Evaluated per-request by the guard evaluator.
    """

    requirements: tuple[GuardRequirement, ...]
    route_name: str = ""  # For logging/startup validation

    @property
    def requirement_names(self) -> tuple[str, ...]:
        return tuple(r.name for r in self.requirements)


# Sentinel for redirect URL storage in GuardContext.metadata
_REDIRECT_URL_KEY = "_guard_redirect_url"


@dataclass
class GuardContext:
    """Mutable context accumulated during guard evaluation.

    Attached to request.guard after all requirements pass.
    Resource resolvers store their results here so downstream
    requirements and the handler can access resolved objects.
    """

    resources: dict[str, object] = field(default_factory=dict)
    metadata: dict[str, object] = field(default_factory=dict)

    def __getattr__(self, name: str) -> object:
        # Allow request.guard.forum instead of request.guard.resources["forum"]
        resources = object.__getattribute__(self, "resources")
        if name in resources:
            return resources[name]
        msg = f"GuardContext has no resource '{name}'. Available: {', '.join(resources) or '(none)'}"
        raise AttributeError(msg)

    def __repr__(self) -> str:
        resource_keys = list(self.resources)
        return f"GuardContext(resources={resource_keys}, metadata_keys={list(self.metadata)})"
