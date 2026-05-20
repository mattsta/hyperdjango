"""
View shortcuts — convenience functions for common view patterns.

Django-equivalent shortcuts adapted for HyperDjango's async-first architecture.
All functions work with HyperDjango's Model, QuerySet, Request, and Response classes.

Usage:
    from hyperdjango.shortcuts import render, redirect, get_object_or_404

    @app.get("/articles/{id}")
    async def article_detail(request, id: int):
        article = await get_object_or_404(Article, id=id)
        return render(request, "articles/detail.html", {"article": article})

    @app.post("/articles/{id}/delete")
    async def article_delete(request, id: int):
        article = await get_object_or_404(Article, id=id)
        await article.delete()
        return redirect("/articles/")
"""

import functools

from hyperdjango.exceptions import HTTPException
from hyperdjango.response import Response


async def get_object_or_404(model_class, db=None, **kwargs):
    """Get a single model instance or raise HTTP 404.

    Equivalent to Django's get_object_or_404(). Calls QuerySet.get()
    and converts DoesNotExist/MultipleObjectsReturned to 404.

    Args:
        model_class: Model class to query.
        db: Optional database connection.
        **kwargs: Filter arguments passed to QuerySet.filter().get().

    Returns:
        Model instance.

    Raises:
        HTTPException: 404 if object not found or multiple found.

    Usage:
        article = await get_object_or_404(Article, id=42)
        article = await get_object_or_404(Article, slug="hello-world")
    """
    try:
        return await model_class.objects.filter(**kwargs).get(db=db)
    except model_class.DoesNotExist:
        raise HTTPException(
            404,
            f"{model_class.__name__} matching query does not exist.",
        )
    except model_class.MultipleObjectsReturned:
        raise HTTPException(
            404,
            f"Multiple {model_class.__name__} objects returned.",
        )


async def get_list_or_404(model_class, db=None, **kwargs):
    """Get a list of model instances or raise HTTP 404 if empty.

    Equivalent to Django's get_list_or_404(). Calls QuerySet.filter()
    and raises 404 if the result is empty.

    Args:
        model_class: Model class to query.
        db: Optional database connection.
        **kwargs: Filter arguments passed to QuerySet.filter().

    Returns:
        List of model instances.

    Raises:
        HTTPException: 404 if no objects match.

    Usage:
        articles = await get_list_or_404(Article, published=True)
    """
    results = await model_class.objects.filter(**kwargs).all(db=db)
    if not results:
        raise HTTPException(
            404,
            f"No {model_class.__name__} objects match the given query.",
        )
    return results


def redirect(
    to: str, *, permanent: bool = False, status: int | None = None
) -> Response:
    """Create a redirect response.

    Equivalent to Django's redirect(). Returns an HTTP redirect response.

    Args:
        to: URL string to redirect to.
        permanent: If True, use 301 (permanent redirect). Default 302 (temporary).
        status: Explicit status code override. If provided, overrides `permanent`.

    Returns:
        Response with redirect status and Location header.

    Usage:
        return redirect("/articles/")
        return redirect("/new-location/", permanent=True)
        return redirect("/articles/42/", status=307)  # preserve method
    """
    if status is None:
        status = 301 if permanent else 302
    return Response.redirect(to, status=status)


def render(
    request,
    template_name: str,
    context: dict[str, object] | None = None,
    *,
    status: int = 200,
    content_type: str = "text/html; charset=utf-8",
) -> Response:
    """Render a template with context and return an HTML response.

    Equivalent to Django's render(). Loads the template, renders it with
    the given context, and returns a Response.

    Requires that the app has a template engine configured (templates_dir
    or explicit TemplateEngine).

    Args:
        request: The current Request object. Used to access the app's template engine.
        template_name: Path to the template file (e.g., "articles/detail.html").
        context: Dictionary of template variables. Defaults to empty dict.
        status: HTTP status code. Default 200.
        content_type: Response content type. Default "text/html; charset=utf-8".

    Returns:
        Response with rendered HTML body.

    Usage:
        return render(request, "articles/list.html", {"articles": articles})
        return render(request, "errors/custom.html", {"error": msg}, status=400)
    """
    # Access the app's render method via the request
    app = request.app
    return app.render(template_name, context, status=status)


# ── HTTP method decorators ─────────────────────────────────────────────────


def require_http_methods(methods: list[str]):
    """Decorator that restricts a view to specific HTTP methods.

    Returns 405 Method Not Allowed with an Allow header if the method
    doesn't match.

    Usage:
        @app.route("/items/{id}")
        @require_http_methods(["GET", "POST"])
        async def item_view(request, id: int):
            ...
    """
    allowed = {m.upper() for m in methods}
    allow_header = ", ".join(sorted(allowed))

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(request, *args, **kwargs):
            if request.method not in allowed:
                # Unified error body; the allowed methods are carried by the
                # HTTP-correct Allow header, not a bespoke body field.
                return Response.error(405, headers={"Allow": allow_header})
            return await func(request, *args, **kwargs)

        return wrapper

    return decorator


def require_GET(func):
    """Decorator that restricts a view to GET (and HEAD) only."""
    return require_http_methods(["GET", "HEAD"])(func)


def require_POST(func):
    """Decorator that restricts a view to POST only."""
    return require_http_methods(["POST"])(func)


def require_safe(func):
    """Decorator that restricts a view to safe methods (GET, HEAD)."""
    return require_http_methods(["GET", "HEAD"])(func)


# ── Security decorators ────────────────────────────────────────────────────


def xframe_options_deny(func):
    """Set X-Frame-Options: DENY on the response."""

    @functools.wraps(func)
    async def wrapper(request, *args, **kwargs):
        response = await func(request, *args, **kwargs)
        response.headers["X-Frame-Options"] = "DENY"
        return response

    return wrapper


def xframe_options_sameorigin(func):
    """Set X-Frame-Options: SAMEORIGIN on the response."""

    @functools.wraps(func)
    async def wrapper(request, *args, **kwargs):
        response = await func(request, *args, **kwargs)
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        return response

    return wrapper


def xframe_options_exempt(func):
    """Remove X-Frame-Options header (allow framing from any origin)."""

    @functools.wraps(func)
    async def wrapper(request, *args, **kwargs):
        response = await func(request, *args, **kwargs)
        response.headers.pop("X-Frame-Options", None)
        response.headers.pop("x-frame-options", None)
        return response

    return wrapper


def sensitive_variables(*variables):
    """Mark view variables as sensitive — excluded from error reports.

    Usage:
        @sensitive_variables("password", "credit_card")
        async def process_payment(request):
            password = request.json["password"]
            credit_card = request.json["card"]
            ...
    """

    def decorator(func):
        func._sensitive_variables = variables or "__ALL__"
        return func

    return decorator


def sensitive_post_parameters(*parameters):
    """Mark POST parameters as sensitive — masked in error reports.

    Usage:
        @sensitive_post_parameters("password", "token")
        async def login(request):
            ...
    """

    def decorator(func):
        func._sensitive_post_parameters = parameters or "__ALL__"
        return func

    return decorator


def never_cache(func):
    """Set Cache-Control headers to prevent caching."""

    @functools.wraps(func)
    async def wrapper(request, *args, **kwargs):
        response = await func(request, *args, **kwargs)
        response.headers["Cache-Control"] = (
            "max-age=0, no-cache, no-store, must-revalidate, private"
        )
        response.headers["Expires"] = "0"
        response.headers["Pragma"] = "no-cache"
        return response

    return wrapper


def vary_on_headers(*headers):
    """Add Vary header for cache key variation.

    Usage:
        @vary_on_headers("Cookie", "Accept-Language")
        async def my_view(request):
            ...
    """

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(request, *args, **kwargs):
            response = await func(request, *args, **kwargs)
            existing = response.headers.get("Vary", "")
            new_vary = ", ".join(headers)
            if existing:
                response.headers["Vary"] = f"{existing}, {new_vary}"
            else:
                response.headers["Vary"] = new_vary
            return response

        return wrapper

    return decorator


def vary_on_cookie(func):
    """Add Vary: Cookie header for cache key variation."""
    return vary_on_headers("Cookie")(func)
