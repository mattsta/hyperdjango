"""
HyperGuard @guard_action() — declarative protection for REST ViewSet actions.

Wraps a ViewSet @action method with a compiled requirement chain. Requirements
are evaluated after ViewSet-level permission_classes but before the action handler.

Usage:
    from hyperdjango.guard import guard_action, Require

    class BookViewSet(ModelViewSet):
        @action(methods=["POST"], detail=True, url_path="publish")
        @guard_action(Require.authenticated(), Require.staff())
        async def publish(self, request, **kwargs):
            instance = await self.get_object()
            ...

The @action() decorator must be outermost so ViewSet dispatch sees the action
metadata. @guard_action() wraps the inner method and preserves all action
attributes (_is_action, _action_meta, _action_methods, etc.).
"""

import functools
from collections.abc import Callable

from hyperdjango.exceptions import HTTPException
from hyperdjango.guard.evaluator import _RedirectDenial, evaluate_guard
from hyperdjango.guard.types import GuardRequirement, GuardSpec

# Action metadata attributes set by @action() that must be forwarded
# through the guard wrapper so ViewSet dispatch sees them.
_ACTION_ATTRS = (
    "_is_action",
    "_action_methods",
    "_action_detail",
    "_action_url_path",
    "_action_url_name",
    "_action_meta",
)


def guard_action(*requirements: GuardRequirement) -> Callable:
    """Decorator that enforces a guard specification on a ViewSet action.

    Works like @guard() but designed for ViewSet @action methods:
    - Evaluates after ViewSet permission_classes (already checked by dispatch)
    - Denials raise HTTPException caught by ViewSet error handler → JSON response
    - No redirect support (API actions return JSON, not redirects)
    - Preserves @action metadata for ViewSet route registration

    Args:
        *requirements: One or more GuardRequirement instances from Require.* factories.

    Returns:
        Decorator that wraps the action method.
    """
    spec = GuardSpec(requirements=tuple(requirements))

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(self, request, *args, **kwargs):
            try:
                ctx = await evaluate_guard(request, spec)
            except _RedirectDenial:
                # API actions don't redirect — convert to 401 JSON response.
                # This happens if Require.authenticated(redirect_url=...) is
                # mistakenly used with guard_action.
                raise HTTPException(401, "Authentication required")
            request.guard = ctx
            return await func(self, request, *args, **kwargs)

        # Tag for scanner detection
        wrapper._guard_spec = spec

        # Forward @action attributes so both decorator orderings work.
        # Normal usage: @action (outermost) sets attrs directly on guard_wrapper.
        # Reversed usage: @action (innermost) set attrs on func; copy them here.
        for attr in _ACTION_ATTRS:
            if attr in func.__dict__:
                wrapper.__dict__[attr] = func.__dict__[attr]

        return wrapper

    return decorator
