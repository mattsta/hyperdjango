"""
REGRESSION (Round 11): WebSocket auth must apply the HTTP identity gate.

Both WS auth entry points now gate on _is_user_session BEFORE promoting a
store session to a SessionUser, mirroring the HTTP path (auth/sessions.py:582).

  - hyperdjango/guard/websocket.py :: _authenticate_ws
  - hyperdjango/realtime.py        :: _resolve_ws_principal

Assertions:
  1. A legitimately-signed ANONYMOUS session (flash-only, no user_id/id/pk/
     username) is REJECTED by BOTH paths (returns None -> guard closes / 4001).
  2. A login-shaped session (user_id set) is ACCEPTED with the correct
     SessionUser identity.

Run: python scripts/test_ws_auth_anon_gate_r11.py
"""

# hyper-test: unit

import asyncio
import traceback

from hyperdjango.auth.sessions import (
    InMemorySessionStore,
    SessionAuth,
    _is_user_session,
)
from hyperdjango.guard.websocket import _authenticate_ws
from hyperdjango.realtime import _resolve_ws_principal
from hyperdjango.testkit import check, finish, run_main


class _FakeWS:
    """Minimal stand-in for both WS auth paths.

    _authenticate_ws reads .headers['cookie']; _resolve_ws_principal reads the
    token from ?token= (.query_string) or the cookie header.
    """

    def __init__(
        self, cookie_header: str = "", query_string: str = "", path: str = "/ws/chat"
    ):
        self.headers = {"cookie": cookie_header}
        self.query_string = query_string
        self.path = path


async def _run() -> None:
    store = InMemorySessionStore()
    auth = SessionAuth(secret="test-secret-key", store=store)

    # ---- ANONYMOUS (flash-only) session: must be REJECTED ------------------
    anon_data = {"_messages": [{"level": "info", "text": "please log in"}]}
    assert _is_user_session(anon_data) is False, (
        "anon session must not be a user session"
    )
    anon_sid = store.create(anon_data)
    anon_signed = auth._sign_session_id(anon_sid)  # legitimately signed

    anon_ws_cookie = _FakeWS(cookie_header=f"{auth.cookie_name}={anon_signed}")
    anon_ws_query = _FakeWS(query_string=f"token={anon_signed}")

    anon_via_guard = await _authenticate_ws(anon_ws_cookie, auth)
    anon_via_realtime = await _resolve_ws_principal(
        anon_ws_query, auth, allow_insecure_raw_token=False
    )

    assert anon_via_guard is None, (
        f"BYPASS: _authenticate_ws accepted an anonymous session: {anon_via_guard!r}"
    )
    assert anon_via_realtime is None, (
        f"BYPASS: _resolve_ws_principal accepted an anonymous session: {anon_via_realtime!r}"
    )
    check("anonymous flash-only session REJECTED by both WS paths", True)

    # ---- LOGIN-shaped session: must be ACCEPTED ----------------------------
    # user_id is the identity key that _is_user_session gates on; id/username
    # carry the resolved principal identity (SessionUser.id reads id/pk).
    user_data = {"user_id": 42, "id": 42, "username": "alice"}
    assert _is_user_session(user_data) is True, "login session must be a user session"
    user_sid = store.create(user_data)
    user_signed = auth._sign_session_id(user_sid)

    user_ws_cookie = _FakeWS(cookie_header=f"{auth.cookie_name}={user_signed}")
    user_ws_query = _FakeWS(query_string=f"token={user_signed}")

    user_via_guard = await _authenticate_ws(user_ws_cookie, auth)
    user_via_realtime = await _resolve_ws_principal(
        user_ws_query, auth, allow_insecure_raw_token=False
    )

    assert user_via_guard is not None, (
        "login session wrongly rejected by _authenticate_ws"
    )
    assert user_via_guard.is_authenticated is True
    assert user_via_guard.id == 42, f"unexpected id: {user_via_guard.id!r}"
    assert user_via_guard.username == "alice"

    assert user_via_realtime is not None, (
        "login session wrongly rejected by _resolve_ws_principal"
    )
    assert user_via_realtime.is_authenticated is True
    assert user_via_realtime.id == 42, f"unexpected id: {user_via_realtime.id!r}"
    assert user_via_realtime.username == "alice"
    check("login-shaped session ACCEPTED by both WS paths with correct identity", True)


def main() -> bool:
    try:
        asyncio.run(_run())
    except Exception as exc:
        traceback.print_exc()
        check("ws_auth_anon_gate", False, f"{type(exc).__name__}: {exc}")
        finish()
        return False
    print()
    return finish()


if __name__ == "__main__":
    run_main(main)
