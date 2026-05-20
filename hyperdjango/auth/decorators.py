"""
Auth decorators for HyperApp routes.
"""

import functools

from hyperdjango.conf import get_setting
from hyperdjango.exceptions import HTTPException
from hyperdjango.response import Response


def _is_authenticated(request):
    """Default auth check — supports both User objects and session dicts.

    request.user is always None, AnonymousUser, User, or SessionUser.
    All non-None user types expose .is_authenticated.
    """
    user = request.user
    if user is None:
        return False
    return user.is_authenticated


def require_auth(auth_check=None, *, login_url=None, redirect_unauthenticated=True):
    """Decorator that requires authentication.

    Unauthenticated requests are redirected to LOGIN_URL (from conf settings)
    unless redirect_unauthenticated=False, in which case a 401 is raised.

    Usage:
        @app.get("/protected")
        @require_auth()
        async def protected(request):
            return {"user": request.user}

        # Custom login URL:
        @require_auth(login_url="/auth/signin/")
        async def protected(request):
            ...

        # Or with a custom check:
        @require_auth(lambda r: r.api_key_valid)
        async def api_endpoint(request):
            ...
    """
    if auth_check is None:
        auth_check_fn = _is_authenticated
    elif callable(auth_check) and not hasattr(auth_check, "__wrapped__"):
        auth_check_fn = auth_check
    else:
        auth_check_fn = _is_authenticated

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(request, *args, **kwargs):
            if not auth_check_fn(request):
                if redirect_unauthenticated:
                    target = (
                        login_url if login_url is not None else get_setting("LOGIN_URL")
                    )
                    return Response.redirect(target, status=302)
                raise HTTPException(401, "Authentication required")
            return await func(request, *args, **kwargs)

        return wrapper

    return decorator


def require_staff(func):
    """Decorator that requires is_staff=True."""

    @functools.wraps(func)
    async def wrapper(request, *args, **kwargs):
        user = request.user
        if user is None:
            raise HTTPException(401, "Authentication required")
        if not user.is_authenticated:
            raise HTTPException(401, "Authentication required")
        if not user.is_staff:
            raise HTTPException(403, "Staff access required")
        return await func(request, *args, **kwargs)

    return wrapper


def require_permission(*perms, model=None):
    """Decorator that requires specific permissions.

    Args:
        model: Optional model name to SCOPE the codenames to. Without it a bare
            codename matches any model that has it, so a codename reused across
            models (e.g. "publish" on both Article and Invoice) bleeds — an
            Article-publisher would pass an Invoice check. Pass ``model=...`` (or
            fully-qualify a perm as ``"model.codename"``) to bind the check to
            one model.

    Usage:
        @require_permission("add_product")
        async def create_product(request):
            ...

        @require_permission("publish", model="article")
        async def publish_article(request):
            ...

        @require_permission("invoice.publish")   # fully-qualified, self-scoping
        async def publish_invoice(request):
            ...
    """

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(request, *args, **kwargs):
            user = request.user
            if user is None:
                raise HTTPException(401, "Authentication required")
            if not user.is_authenticated:
                raise HTTPException(401, "Authentication required")

            # Superuser bypasses all checks
            if user.is_superuser and user.is_active:
                return await func(request, *args, **kwargs)

            # _perm_checker is a declared Request field (default None), set by
            # the auth middleware at request time. Unset (no middleware) → None →
            # 403 below, exactly as before.
            checker = request._perm_checker
            if checker is None:
                raise HTTPException(403, "Permission system not configured")

            for perm in perms:
                if not await checker.has_perm(user, perm, model):
                    raise HTTPException(403, f"Permission denied: {perm}")

            return await func(request, *args, **kwargs)

        return wrapper

    return decorator


def require_api_key(func):
    """Decorator that requires a valid API key."""

    @functools.wraps(func)
    async def wrapper(request, *args, **kwargs):
        if not request.api_key_valid:
            raise HTTPException(401, "Valid API key required")
        return await func(request, *args, **kwargs)

    return wrapper
