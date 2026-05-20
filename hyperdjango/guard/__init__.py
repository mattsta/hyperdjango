"""
HyperGuard — compiled permission DSL for HyperDjango.

Declarative route protection via requirement chains compiled at decoration time.

Available requirements:
    Require.authenticated(redirect_url=...)  — session auth (dict with 'id')
    Require.staff()                          — RBAC "staff" group / is_staff / timeline
    Require.group("editors")                 — RBAC group membership from session
    Require.superuser()                      — is_superuser=True
    Require.not_banned()                     — is_banned not True
    Require.not_muted()                      — is_muted not True
    Require.api_key()                        — valid API key (APIKeyAuth middleware)
    Require.resource(key, resolver=..., from_path=...)  — async resource resolution
    Require.check(name, fn=...)              — custom async check
    Require.any_of(req1, req2, ...)          — OR composition
    Require.policy("Resource.action", registry=...)     — evaluate compiled .guard policy

Zig-accelerated bytecode evaluation:
    compile_conditions([Condition(...), ...]) → CompiledGuard
    compiled.evaluate(user_dict, resource_dict) → bool  (sub-μs via Zig)

SQL generation from policies:
    generate_where(resource, action, user_fields=...) → SQLFragment
    QuerySet.guard_filter(user, action, registry=...) → QuerySet with WHERE clause

Route scanning at startup:
    from hyperdjango.guard.scanner import scan_routes, log_guard_summary
    result = scan_routes(app)
    log_guard_summary(result)
"""

from hyperdjango.guard.action import guard_action
from hyperdjango.guard.decorator import guard
from hyperdjango.guard.requirements import Require
from hyperdjango.guard.types import (
    DenyReason,
    GuardContext,
    GuardDenial,
    GuardRequirement,
    GuardSpec,
    RequirementKind,
)
from hyperdjango.guard.websocket import guard_websocket

# Compiler, SQL, and registry types are lazy-loaded to avoid requiring
# native extension for basic guard usage (Require, @guard decorator).


def __getattr__(name: str) -> object:
    _COMPILER_NAMES = {
        "compile_conditions",
        "CompiledGuard",
        "Condition",
        "CrossFieldCondition",
        "CondOp",
        "CondSource",
        "CombineMode",
    }
    if name in _COMPILER_NAMES:
        from hyperdjango.guard import compiler

        return compiler.__dict__[name]

    _SQL_NAMES = {"SQLFragment", "generate_where"}
    if name in _SQL_NAMES:
        from hyperdjango.guard import sql

        return sql.__dict__[name]

    _REGISTRY_NAMES = {"PolicyRegistry"}
    if name in _REGISTRY_NAMES:
        from hyperdjango.guard import registry

        return registry.__dict__[name]

    msg = f"module 'hyperdjango.guard' has no attribute {name!r}"
    raise AttributeError(msg)


__all__ = [
    "guard",
    "guard_action",
    "guard_websocket",
    "Require",
    "DenyReason",
    "GuardContext",
    "GuardDenial",
    "GuardRequirement",
    "GuardSpec",
    "RequirementKind",
    "compile_conditions",
    "CompiledGuard",
    "Condition",
    "CrossFieldCondition",
    "CondOp",
    "CondSource",
    "CombineMode",
    "SQLFragment",
    "generate_where",
    "PolicyRegistry",
]
