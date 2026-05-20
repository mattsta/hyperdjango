"""
HTTP exceptions for HyperDjango — the single, unified error hierarchy.

Separated from app.py to avoid circular imports — auth.decorators needs
HTTPException but app.py imports auth.sessions.

``HTTPException`` is the ONE base for every HTTP error the framework raises.
REST's ``APIException`` (and its subclasses) extend it, so a single mapper —
``exception_to_response`` — turns any of them into an identical response shape
no matter where it is raised (plain handler, middleware, serializer, guard, or
viewset). Never import ``hyperdjango.rest`` from here: the dependency runs one
way only (rest → exceptions), and the mapper's ``Response``/settings imports are
deferred to call time to keep this module free of import cycles.

Foreign / builtin exceptions declare their HTTP status one of two ways, both
resolved by ``exception_to_response`` — so a plain handler that lets a
``PermissionError`` or ``Model.DoesNotExist`` escape yields a correct 403/404,
never a misleading 500:

  1. an ``http_status`` int attribute on the class/instance (the protocol hook), or
  2. a push-registration via ``register_exception_status(ExcType, status)`` placed
     next to the class definition (no central isinstance chain, no import cycle).
"""

import http
import logging

_logger = logging.getLogger("hyperdjango")


def _reason(status: int) -> str:
    """The standard HTTP reason phrase for ``status`` (the SAFE generic detail
    used when a mapped exception's own message must not be surfaced)."""
    try:
        return http.HTTPStatus(status).phrase
    except ValueError:
        return "Error"


class HTTPException(Exception):
    """HTTP exception that produces an error response.

    The single base for all HTTP errors (REST's ``APIException`` subclasses
    this). Carries the status code, human-readable detail, optional response
    ``headers`` (e.g. ``Retry-After`` on a 429), and optional structured
    ``errors`` (field → messages, used by validation failures).
    """

    def __init__(self, status_code, detail="", headers=None, errors=None):
        self.status_code = status_code
        self.detail = detail
        self.headers = headers or {}
        self.errors = errors
        super().__init__(detail)


# ── Exception → HTTP status registry (the ONE mapping authority) ──────────────
# Maps a NON-HTTPException type to (status, safe_detail). ``safe_detail=True``
# means the exception's own ``str()`` is safe to surface to the client (e.g. a
# validation or not-found message); otherwise the standard HTTP reason phrase
# (via ``_reason``) is used and the specifics go only to the log. MRO-walked, so a
# base registration (e.g. ``Model.DoesNotExist``) covers every subclass.
_EXC_STATUS: dict[type, tuple[int, bool]] = {}


def register_exception_status(exc_type, status, *, safe_detail=False):
    """Declare the HTTP status a (non-HTTPException) exception type maps to.

    Call this next to the exception's class definition. Idempotent.
    """
    _EXC_STATUS[exc_type] = (int(status), bool(safe_detail))


def _resolve_status(exc):
    """Resolve (status, detail, errors, headers) for any exception, or None to
    fall through to the generic 500."""
    # (1) HTTPException / APIException — the intentional-result fast path.
    if isinstance(exc, HTTPException):
        return exc.status_code, exc.detail, exc.errors, (exc.headers or None)

    # (2) Protocol hook: an exception may declare its own status.
    hook = getattr(
        type(exc), "http_status", None
    )  # dynamic-attr: optional cross-library status-declaration protocol on an arbitrary exception class
    if isinstance(hook, int):
        detail = str(exc) or _reason(hook)
        return hook, detail, None, None

    # (3) Registry (MRO-walked): the first registered base wins.
    for klass in type(exc).__mro__:
        entry = _EXC_STATUS.get(klass)
        if entry is not None:
            status, safe_detail = entry
            detail = str(exc) if (safe_detail and str(exc)) else _reason(status)
            return status, detail, None, None

    return None


def exception_to_response(exc):
    """Map any exception to a ``Response`` — the ONE error-response policy.

    Resolution order: ``HTTPException``/``APIException`` status → an
    ``http_status`` protocol attribute → the ``register_exception_status``
    registry (MRO-walked) → generic 500. Body is always ``{"detail","status"}``
    (+ ``errors`` when carried); ``headers`` (e.g. ``Retry-After``) are forwarded.

    A registry/hook-mapped exception's own message is surfaced as ``detail`` only
    when the registration marked it ``safe_detail`` (validation/not-found); an
    unmapped exception NEVER leaks internals (message/type/traceback) — its
    generic 500 detail is deterministic regardless of ``DEBUG``. Rich debug
    output is surfaced separately by the ASGI dispatch's HTML debug page
    (gated on the app's ``debug`` flag).
    """
    # Deferred import: keep this module import-cycle-free (response sits
    # "above" exceptions in the import graph).
    from hyperdjango.response import Response

    resolved = _resolve_status(exc)
    if resolved is not None:
        status, detail, errors, headers = resolved
        body = {"detail": detail, "status": status}
        if errors is not None:
            body["errors"] = errors
        # A mapped non-5xx that carried a real cause: log it for the operator
        # (the client only sees the safe body).
        if status >= 500:
            _logger.exception("Unhandled error in handler")
        return Response.json(body, status=status, headers=headers)

    _logger.exception("Unhandled error in handler")
    return Response.json({"detail": "Internal Server Error", "status": 500}, status=500)


# The builtin PermissionError is a 403 wherever it escapes to the boundary.
register_exception_status(PermissionError, 403)
