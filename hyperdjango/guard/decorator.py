"""
HyperGuard @guard() decorator — declarative route protection.

Wraps a route handler with a compiled requirement chain. Requirements
are evaluated in order before the handler runs. Resolved resources
are available via request.guard.<name>.

Usage:
    from hyperdjango.guard import guard, Require

    @app.post("/f/{forum_name}/submit")
    @guard(
        Require.authenticated(redirect_url="/login"),
        Require.resource("forum", resolver=resolve_forum_write, from_path="forum_name"),
    )
    async def forum_submit(request, forum_name: str):
        forum = request.guard.forum  # Already resolved and validated
        ...
"""

import functools
from collections.abc import Callable

from hyperdjango.guard.evaluator import (
    _RedirectDenial,
    build_redirect_response,
    evaluate_guard,
)
from hyperdjango.guard.types import GuardRequirement, GuardSpec


def guard(*requirements: GuardRequirement) -> Callable:
    """Decorator that enforces a guard specification on a route handler.

    Requirements are evaluated in declaration order, short-circuiting on
    first failure. On success, request.guard is set to a GuardContext
    containing all resolved resources.

    Args:
        *requirements: One or more GuardRequirement instances from Require.* factories.

    Returns:
        Decorator that wraps the route handler.
    """
    spec = GuardSpec(
        requirements=tuple(requirements),
    )

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(request, *args, **kwargs):
            try:
                ctx = await evaluate_guard(request, spec)
            except _RedirectDenial as exc:
                return build_redirect_response(exc)
            # Attach guard context to request
            request.guard = ctx
            return await func(request, *args, **kwargs)

        # Tag the wrapper so startup validation can detect guarded routes
        wrapper._guard_spec = spec
        return wrapper

    return decorator
