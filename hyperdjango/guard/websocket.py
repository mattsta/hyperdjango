"""
HyperGuard @guard_websocket() — declarative protection for WebSocket handlers.

WebSocket connections don't go through HTTP middleware, so session auth must
be done manually by parsing the cookie header. @guard_websocket() handles
authentication and evaluates the full Require.* chain before the handler runs.

Usage:
    from hyperdjango.guard import guard_websocket, Require

    @app.websocket("/ws/chat")
    @guard_websocket(
        auth,  # SessionAuth instance
        Require.authenticated(),
        Require.not_banned(),
    )
    async def chat(ws):
        user = ws.user        # Authenticated user dict
        guard = ws.guard      # GuardContext
        ...

On denial:
    - Accepts the WebSocket (required by ASGI protocol before close)
    - Sends error JSON: {"type": "error", "message": "..."}
    - Closes with code: 4001 (auth), 4003 (forbidden), 4004 (not found)
"""

import asyncio
import functools
from collections.abc import Callable
from dataclasses import dataclass, field

from hyperdjango.auth.sessions import SessionAuth, _is_user_session
from hyperdjango.auth.user import SessionUser
from hyperdjango.exceptions import HTTPException
from hyperdjango.guard.evaluator import _RedirectDenial, evaluate_guard
from hyperdjango.guard.types import GuardRequirement, GuardSpec
from hyperdjango.websocket import is_ws_origin_allowed

# Map HTTP status codes to WebSocket close codes
_STATUS_TO_WS_CODE: dict[int, int] = {
    401: 4001,
    403: 4003,
    404: 4004,
    429: 4029,
}


@dataclass(frozen=True)
class _WSRequest:
    """Lightweight request-like object for guard evaluation over WebSocket.

    Carries the user dict and path/method info extracted from the WebSocket
    connection. Guard requirement evaluators access request.user, request.path,
    request.method, and request.path_params — this provides all of them.
    """

    user: dict[str, object] | None
    path: str
    method: str = "WEBSOCKET"
    path_params: dict[str, str] = field(default_factory=dict)
    api_key_valid: bool = False


def _parse_cookie(header: str, name: str) -> str | None:
    """Extract a named cookie value from a raw Cookie header."""
    for part in header.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        k, _, v = part.partition("=")
        if k.strip() == name:
            val = v.strip()
            if val:
                return val
    return None


async def _authenticate_ws(ws: object, session_auth: SessionAuth) -> SessionUser | None:
    """Authenticate a WebSocket connection from the session cookie.

    Parses the Cookie header, verifies the signed session ID, and looks
    up the session in the session store. Returns a SessionUser or None.
    """
    cookie_header = ws.headers.get("cookie", "")
    if not cookie_header:
        return None
    cookie_val = _parse_cookie(cookie_header, session_auth.cookie_name)
    if not cookie_val:
        return None
    session_id = session_auth._verify_session_cookie(cookie_val)
    if not session_id:
        return None
    result = session_auth.store.get(session_id)
    if asyncio.iscoroutine(result) or asyncio.isfuture(result):
        result = await result
    if result is None:
        return None
    # Identity gate — mirror the HTTP path (auth/sessions.py: SessionAuth only
    # promotes to SessionUser when _is_user_session(data)). A legitimately-signed
    # ANONYMOUS session (flash/cart/wizard state, no user_id/id/pk/username) must
    # NOT authenticate: fail closed so the guard denies exactly as a missing one.
    if not _is_user_session(result):
        return None
    return SessionUser(result)


def guard_websocket(
    session_auth: SessionAuth,
    *requirements: GuardRequirement,
) -> Callable:
    """Decorator that enforces a guard specification on a WebSocket handler.

    First authenticates the WebSocket connection from the session cookie,
    then evaluates the requirement chain. On success, sets ws.user and
    ws.guard before calling the handler.

    Args:
        session_auth: SessionAuth instance for cookie parsing and session lookup.
        *requirements: One or more GuardRequirement instances from Require.* factories.

    Returns:
        Decorator that wraps the WebSocket handler.
    """
    spec = GuardSpec(requirements=tuple(requirements))

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(ws, *args, **kwargs):
            # Accept first — ASGI requires accept before close.
            # If the client already disconnected, accept() may raise;
            # return silently since there's no connection to respond to.
            try:
                await ws.accept()
            # The client may have already disconnected before we could accept —
            # there is no live connection to authenticate, guard, or send an
            # error to, so return silently and let the socket close.
            # blind-except: peer gone before accept() — nothing to respond to.
            except Exception:
                return

            # CSWSH defense: reject a disallowed cross-origin handshake before
            # authenticating (same policy as ws_authenticated — one authority).
            if not is_ws_origin_allowed(ws):
                await ws.close(4403, "Origin not allowed")
                return

            # Authenticate from session cookie
            user = await _authenticate_ws(ws, session_auth)

            # Build a request-like object for guard evaluation.
            # kwargs from the router contain path params (e.g., room_id from /ws/{room_id}).
            ws_request = _WSRequest(
                user=user,
                path=ws.path,
                path_params={k: str(v) for k, v in kwargs.items()},
            )

            # Evaluate guard chain
            try:
                ctx = await evaluate_guard(ws_request, spec)
            except _RedirectDenial:
                # WebSocket handlers don't redirect — convert to 4001 close.
                await ws.send_json(
                    {"type": "error", "message": "Authentication required"}
                )
                await ws.close(4001, "Authentication required")
                return
            except HTTPException as exc:
                ws_code = _STATUS_TO_WS_CODE.get(exc.status_code, 4003)
                await ws.send_json({"type": "error", "message": exc.detail})
                await ws.close(ws_code, exc.detail)
                return

            # Attach user and guard context to the WebSocket
            ws.user = user
            ws.guard = ctx
            return await func(ws, *args, **kwargs)

        # Tag for scanner detection
        wrapper._guard_spec = spec
        return wrapper

    return decorator
