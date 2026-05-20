"""
Round-9 WebSocket security + lifecycle regression tests.

Covers the fixes for:

  #1 (HIGH SECURITY) realtime.ws_authenticated must VERIFY client identity
     (HMAC signature over the session id + session-store lookup), not trust the
     raw ?token= / cookie value. An unsigned/forged token is rejected; only a
     validly-signed, store-backed session authenticates, and the handler
     receives the VERIFIED principal (a SessionUser), never the raw input.

  #2 (lifecycle) websocket.ZigWebSocket._wait_readable enforces a receive-side
     idle deadline (pong_timeout) so a half-open peer (gone without FIN) is
     reclaimed instead of parking the receive coroutine + fd forever.

Pure-Python: no native rebuild needed. The fd-reuse race fix (#3) and the
channels double-delivery fix (#4) are exercised by their own suites / require
the native .so; assertions that would need the rebuild are noted in the output.
"""

# hyper-test: unit

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hyperdjango.auth.sessions import InMemorySessionStore, SessionAuth
from hyperdjango.native._crypto import sign_data
from hyperdjango.realtime import ws_authenticated
from hyperdjango.websocket import WebSocketDisconnect, ZigWebSocket

PASS = 0
FAIL = 0


def ok(name: str) -> None:
    global PASS
    PASS += 1
    print(f"  PASS: {name}")


def fail(name: str, msg: str = "") -> None:
    global FAIL
    FAIL += 1
    detail = f" -- {msg}" if msg else ""
    print(f"  FAIL: {name}{detail}")


def check(cond: bool, name: str, msg: str = "") -> None:
    ok(name) if cond else fail(name, msg)


def run(coro):
    return asyncio.run(coro)


class MockWebSocket:
    """Minimal WebSocket double: query_string + headers + accept/close."""

    def __init__(self, query_string: str = "", headers: dict[str, str] | None = None):
        self.query_string = query_string
        self.headers = headers or {}
        self.accepted = False
        self.closed = False
        self.close_code = 0

    async def accept(self, subprotocol: str | None = None) -> None:
        self.accepted = True

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True
        self.close_code = code


# ---------------------------------------------------------------------------
# #1 — ws_authenticated verifies identity
# ---------------------------------------------------------------------------

SECRET = "r9-test-secret-key"


def _fresh_auth() -> tuple[SessionAuth, InMemorySessionStore]:
    store = InMemorySessionStore()
    auth = SessionAuth(secret=SECRET, store=store)
    return auth, store


def test_cross_origin_ws_rejected() -> None:
    """CSWSH: a cross-origin handshake is refused (close 4403) before auth."""
    print("\n=== CSWSH cross-origin handshake rejected ===")
    auth, store = _fresh_auth()
    called: list[object] = []

    @ws_authenticated(session_auth=auth)
    async def handler(ws, user):
        called.append(user)

    # A hijacking page's Origin differs from Host → rejected before any auth.
    ws = MockWebSocket(
        query_string="token=whatever",
        headers={"origin": "https://evil.example", "host": "app.example"},
    )
    run(handler(ws))
    check(not called, "cross-origin handshake never reaches handler")
    check(ws.closed and ws.close_code == 4403, "cross-origin closed with 4403")

    # Same-origin is allowed through to auth (which then rejects the bad token 4001).
    ws2 = MockWebSocket(
        query_string="token=whatever",
        headers={"origin": "https://app.example", "host": "app.example"},
    )
    run(handler(ws2))
    check(ws2.close_code == 4001, "same-origin proceeds to auth (4001 on bad token)")


def test_forged_query_token_rejected() -> None:
    print("\n=== #1 forged ?token= rejected ===")
    auth, _ = _fresh_auth()
    called: list[object] = []

    @ws_authenticated(session_auth=auth)
    async def handler(ws, user):
        called.append(user)

    # The classic exploit: ?token=admin with no signature.
    ws = MockWebSocket(query_string="token=admin")
    run(handler(ws))
    check(not called, "forged raw token does NOT reach handler")
    check(ws.closed and ws.close_code == 4001, "forged token closed with 4001")

    # A well-formed-but-wrong signature (signed with a different secret).
    session_id = "sess-xyz"
    bad = sign_data(session_id, "attacker-secret")
    ws2 = MockWebSocket(query_string=f"token={bad}")
    run(handler(ws2))
    check(not called, "token signed with wrong secret rejected")
    check(ws2.close_code == 4001, "wrong-secret token closed 4001")


def test_valid_signed_session_accepted() -> None:
    print("\n=== #1 validly-signed session accepted ===")
    auth, store = _fresh_auth()
    session_id = store.create({"id": 7, "username": "alice"})
    token = sign_data(session_id, SECRET)  # correct HMAC over the session id

    seen: list[object] = []

    @ws_authenticated(session_auth=auth)
    async def handler(ws, user):
        seen.append(user)

    ws = MockWebSocket(query_string=f"token={token}")
    run(handler(ws))
    check(len(seen) == 1, "valid signed token reaches handler")
    principal = seen[0] if seen else None
    # The VERIFIED principal, resolved from the store — not the raw token string.
    check(
        principal is not None and principal.id == 7,
        "handler receives verified SessionUser (id=7)",
        f"got {principal!r}",
    )
    check(
        principal is not None and principal.username == "alice",
        "verified principal carries store-backed username",
    )
    check(not ws.closed, "valid session not closed")


def test_valid_signature_unknown_session_rejected() -> None:
    print("\n=== #1 valid signature over missing session rejected ===")
    auth, _ = _fresh_auth()
    # Correctly signed, but the session id was never created / was deleted.
    token = sign_data("never-created", SECRET)
    called: list[object] = []

    @ws_authenticated(session_auth=auth)
    async def handler(ws, user):
        called.append(user)

    ws = MockWebSocket(query_string=f"token={token}")
    run(handler(ws))
    check(not called, "signed-but-absent session rejected (store lookup)")
    check(ws.close_code == 4001, "absent session closed 4001")


def test_valid_signed_cookie_accepted() -> None:
    print("\n=== #1 signed session cookie accepted ===")
    auth, store = _fresh_auth()  # default cookie_name comes from settings
    session_id = store.create({"id": 3, "username": "bob"})
    token = sign_data(session_id, SECRET)

    seen: list[object] = []

    @ws_authenticated(session_auth=auth)
    async def handler(ws, user):
        seen.append(user)

    ws = MockWebSocket(headers={"cookie": f"{auth.cookie_name}={token}"})
    run(handler(ws))
    check(len(seen) == 1 and seen[0].id == 3, "valid signed cookie authenticates")


def test_no_credentials_rejected() -> None:
    print("\n=== #1 no credentials rejected ===")
    auth, _ = _fresh_auth()
    called: list[object] = []

    @ws_authenticated(session_auth=auth)
    async def handler(ws, user):
        called.append(user)

    ws = MockWebSocket()
    run(handler(ws))
    check(not called and ws.close_code == 4001, "no token/cookie closed 4001")


def test_insecure_mode_is_opt_in_and_loud() -> None:
    print("\n=== #1 raw-token mode is opt-in only ===")
    auth, _ = _fresh_auth()
    seen: list[object] = []

    # Explicit opt-in dev flag: raw token trusted WITHOUT verification.
    @ws_authenticated(session_auth=auth, allow_insecure_raw_token=True)
    async def handler(ws, user):
        seen.append(user)

    ws = MockWebSocket(query_string="token=devuser")
    run(handler(ws))
    check(
        seen == ["devuser"],
        "allow_insecure_raw_token=True passes raw value (dev opt-in)",
    )

    # And it is NOT the default — the identical forged token is rejected without
    # the flag (covered by test_forged_query_token_rejected). Assert the default
    # keyword value is False so the secure path is the default.
    import inspect

    sig = inspect.signature(ws_authenticated)
    check(
        sig.parameters["allow_insecure_raw_token"].default is False,
        "allow_insecure_raw_token defaults to False (secure by default)",
    )


# ---------------------------------------------------------------------------
# #2 — receive-side idle deadline finalizes a half-open connection
# ---------------------------------------------------------------------------


def test_idle_deadline_raises_disconnect() -> None:
    print("\n=== #2 idle deadline surfaces disconnect (half-open) ===")

    async def scenario() -> bool:
        # Build a ZigWebSocket WITHOUT running __init__ (which needs native
        # symbols): drive _wait_readable directly. _reader_active=True skips the
        # add_reader syscall so no real fd is touched; the future it awaits is
        # never resolved, mimicking a half-open peer whose fd never goes readable.
        ws = ZigWebSocket.__new__(ZigWebSocket)
        ws._reader_active = True
        ws._readable_fut = None
        ws._fd = -1
        ws._idle_timeout = 0.05  # deadline instead of 120s, for the test

        loop = asyncio.get_running_loop()
        try:
            await ws._wait_readable(loop)
        except WebSocketDisconnect:
            return True
        return False

    raised = run(scenario())
    check(raised, "half-open wait hits pong_timeout -> WebSocketDisconnect(1001)")

    # And with the deadline disabled (<=0), the wait parks (no spurious timeout).
    async def scenario_disabled() -> bool:
        ws = ZigWebSocket.__new__(ZigWebSocket)
        ws._reader_active = True
        ws._readable_fut = None
        ws._fd = -1
        ws._idle_timeout = None  # disabled
        loop = asyncio.get_running_loop()
        try:
            await asyncio.wait_for(ws._wait_readable(loop), 0.1)
        except WebSocketDisconnect:
            return False  # must NOT self-disconnect when disabled
        except TimeoutError:
            return True  # parked as expected — outer wait_for stopped it
        return False

    parked = run(scenario_disabled())
    check(parked, "idle_timeout=None never self-disconnects (unbounded wait)")


def test_config_makes_pong_timeout_effective() -> None:
    print("\n=== #2 pong_timeout config wired to idle deadline ===")
    # WebSocketConfig.current().pong_timeout is what __init__ reads into
    # _idle_timeout. Assert the config surface exposes a positive default so the
    # deadline is active by default (previously the ping/pong config was inert).
    from hyperdjango.websocket import WebSocketConfig

    cfg = WebSocketConfig.current()
    check(
        cfg.pong_timeout > 0,
        "pong_timeout has a positive default (idle deadline active by default)",
        f"pong_timeout={cfg.pong_timeout}",
    )


def main() -> int:
    test_cross_origin_ws_rejected()
    test_forged_query_token_rejected()
    test_valid_signed_session_accepted()
    test_valid_signature_unknown_session_rejected()
    test_valid_signed_cookie_accepted()
    test_no_credentials_rejected()
    test_insecure_mode_is_opt_in_and_loud()
    test_idle_deadline_raises_disconnect()
    test_config_makes_pong_timeout_effective()

    print(f"\n{'=' * 60}")
    print(f"RESULTS: {PASS} passed, {FAIL} failed")
    print("=" * 60)
    print(
        "\nNOTE (native-rebuild-dependent, NOT asserted here):\n"
        "  - #3 fd-reuse race: the post-write_mutex alive re-check in\n"
        "    ws_send/ws_send_bytes/ws_send_text_bytes lives in\n"
        "    zig/src/websocket_server.zig and needs the orchestrator rebuild.\n"
        "  - #2 end-to-end half-open reclaim over a real native fd also needs\n"
        "    the rebuilt .so; this suite proves the Python deadline logic only."
    )
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
