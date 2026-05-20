"""
HyperGuard evaluator — runs a GuardSpec's requirement chain against a request.

Evaluates requirements in order, short-circuiting on first failure.
On success, attaches GuardContext to request.guard.
On failure, raises HTTPException with appropriate status and message.
Denied access is logged for audit trail.
"""

from urllib.parse import quote

from hyperdjango.exceptions import HTTPException
from hyperdjango.guard.types import (
    _REDIRECT_URL_KEY,
    DenyReason,
    GuardContext,
    GuardDenial,
    GuardSpec,
)
from hyperdjango.logging import logger
from hyperdjango.response import Response
from hyperdjango.security import SecurityEvent as _SecurityEvent
from hyperdjango.security import get_security_log as _get_security_log
from hyperdjango.telemetry import metrics as _tel_metrics

# ── Native telemetry (zero cost when disabled) ──────────────────────────────
#
# Guard denial counter labeled by DenyReason — bounded enum (~6 values).
# A second `requirement` label is intentionally NOT used because the
# requirement name is user-defined and unbounded; it lives in the
# audit logger / SecurityLog instead.

_guard_denials_total = _tel_metrics.CounterVec(
    "hyperdjango_guard_denials_total",
    "HyperGuard requirement denials by reason.",
    label_names=("reason",),
)


async def evaluate_guard(request: object, spec: GuardSpec) -> GuardContext:
    """Evaluate all requirements in the spec against the request.

    Returns GuardContext with resolved resources on success.
    Raises HTTPException on first failed requirement.
    Logs denied access for audit trail.
    """
    ctx = GuardContext()

    for requirement in spec.requirements:
        denial = await requirement.evaluate_fn(request, ctx)
        if denial is not None:
            # Native telemetry — bumped first so the metric is recorded
            # even if the audit logger or SecurityLog raises. The reason
            # label is the bounded DenyReason enum value.
            _guard_denials_total.inc_tuple((denial.reason.value,))
            # Audit log: who was denied, why, on which route
            _log_denial(request, requirement.name, denial)
            # SecurityLog: persist permission denial for compliance tracking
            await _log_denial_to_security(request, requirement.name, denial)

            # Special case: redirect for unauthenticated users
            if (
                denial.reason == DenyReason.NOT_AUTHENTICATED
                and _REDIRECT_URL_KEY in ctx.metadata
            ):
                redirect_url = ctx.metadata[_REDIRECT_URL_KEY]
                raise _RedirectDenial(str(redirect_url), request.path)
            status = denial.effective_status
            # 429 producers all carry Retry-After (F3): a guard rate-limit
            # denial forwards its back-off through HTTPException.headers, which
            # the single mapper always emits.
            headers = None
            if status == 429 and denial.retry_after is not None:
                headers = {"Retry-After": str(denial.retry_after)}
            raise HTTPException(status, denial.message, headers=headers)

    return ctx


def _log_denial(request: object, requirement_name: str, denial: GuardDenial) -> None:
    """Log a guard denial for audit trail (local logger)."""
    user = request.user
    user_id = user.id if user is not None else 0
    username = user.username if user is not None else "anonymous"
    logger.info(
        f"[GUARD] DENIED: {request.method} {request.path} "
        f"user={username}({user_id}) "
        f"requirement={requirement_name} "
        f"reason={denial.reason.value} "
        f"status={denial.effective_status}"
    )


async def _log_denial_to_security(
    request: object, requirement_name: str, denial: GuardDenial
) -> None:
    """Persist guard denial to SecurityLog (if configured).

    Uses AUTH_REQUIRED for not-authenticated denials, PERMISSION_DENIED for
    everything else. Safe to call unconditionally — no-op when SecurityLog
    is not configured.
    """
    sec_log = _get_security_log()
    if sec_log is None:
        return
    event = (
        _SecurityEvent.AUTH_REQUIRED
        if denial.reason == DenyReason.NOT_AUTHENTICATED
        else _SecurityEvent.PERMISSION_DENIED
    )
    try:
        await sec_log.log_from_request(
            event,
            request,
            detail=f"requirement={requirement_name} reason={denial.reason.value}",
        )
    # Persisting the denial to SecurityLog must never change or block the guard
    # decision that already happened; a logging failure is warned and swallowed
    # so the denial still returns to the caller.
    # blind-except: audit-log side effect must never break the guard decision.
    except Exception as e:
        logger.warning("SecurityLog.log_from_request failed: {err}", err=e)


class _RedirectDenial(Exception):
    """Internal: signals that the guard wants a redirect, not an error response."""

    def __init__(self, redirect_url: str, original_path: str):
        self.redirect_url = redirect_url
        self.original_path = original_path


def build_redirect_response(exc: _RedirectDenial) -> Response:
    """Build a redirect response from a redirect denial.

    The original_path is URL-encoded to prevent open redirect and parameter injection.
    """
    url = exc.redirect_url
    if exc.original_path and exc.original_path != "/":
        sep = "&" if "?" in url else "?"
        encoded_path = quote(exc.original_path, safe="")
        url = f"{url}{sep}next={encoded_path}"
    return Response.redirect(url, status=302)
