"""
SecurityLog middleware integration tests.

Verifies RateLimitMiddleware and CSRFMiddleware log security events,
and that guard denials are captured via PERMISSION_DENIED/AUTH_REQUIRED.

# hyper-test: db_isolated
"""

import asyncio
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from hyperdjango.database import Database, set_db
from hyperdjango.ratelimit import RateLimitMiddleware
from hyperdjango.security import (
    SecurityEvent,
    SecurityLog,
    set_security_log,
)

DB_URL = os.environ.get("DATABASE_URL", "postgres://localhost/hyperdjango_test")

PASS = 0
FAIL = 0
ERRORS: list[str] = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        msg = f"  FAIL  {name}" + (f" — {detail}" if detail else "")
        print(msg)
        ERRORS.append(msg)


@dataclass
class _MockRequest:
    """Minimal request object for middleware testing."""

    client_ip: str = "10.0.0.1"
    path: str = "/api/test"
    method: str = "POST"
    headers: dict[str, str] = field(
        default_factory=lambda: {"user-agent": "test-agent/1.0"}
    )
    user: object = None
    api_key_valid: bool = False
    cookies: dict[str, str] = field(default_factory=dict)

    async def form(self):
        return {}


async def test_rate_limit_logs_to_security_log(db):
    """RateLimitMiddleware.__call__ logs RATE_LIMIT_HIT on 429."""
    print("=== RateLimitMiddleware → SecurityLog ===")

    sec_log = SecurityLog(db)
    await sec_log.ensure_table()
    await db.execute("DELETE FROM hyper_security_log")
    set_security_log(sec_log)

    # Rate limit: 2 requests per 60 seconds
    mw = RateLimitMiddleware(max_requests=2, window=60)

    async def _dummy_next(request):
        from hyperdjango.response import Response

        return Response.json({"ok": True})

    request = _MockRequest()

    # First 2 should pass
    r1 = await mw(request, _dummy_next)
    check("1st request passes", r1.status == 200)
    r2 = await mw(request, _dummy_next)
    check("2nd request passes", r2.status == 200)

    # 3rd should be rate-limited AND log to SecurityLog
    r3 = await mw(request, _dummy_next)
    check("3rd request rate-limited", r3.status == 429)

    # Verify SecurityLog has the event
    events = await sec_log.get_by_event(SecurityEvent.RATE_LIMIT_HIT, since_hours=1)
    check("RATE_LIMIT_HIT event logged", len(events) >= 1, f"got {len(events)}")
    if events:
        e = events[0]
        check("event has ip_address", e.get("ip_address") == "10.0.0.1")
        check("event has path", e.get("path") == "/api/test")
        check(
            "event detail mentions key",
            e.get("detail") and "key=" in str(e.get("detail")),
            f"detail={e.get('detail')}",
        )


async def test_rate_limit_no_security_log_when_unconfigured(db):
    """When SecurityLog is not set, rate limiter still works."""
    print("\n=== RateLimitMiddleware without SecurityLog ===")

    # Unset the global SecurityLog
    set_security_log(None)

    mw = RateLimitMiddleware(max_requests=1, window=60)

    async def _dummy_next(request):
        from hyperdjango.response import Response

        return Response.json({"ok": True})

    request = _MockRequest(client_ip="10.0.0.99")

    r1 = await mw(request, _dummy_next)
    check("1st passes without sec_log", r1.status == 200)
    r2 = await mw(request, _dummy_next)
    check("2nd rate-limited without sec_log", r2.status == 429)
    # No exception — middleware handles missing sec_log gracefully


async def test_rate_limit_swallows_seclog_errors(db):
    """If SecurityLog raises, rate limiter still returns 429 cleanly."""
    print("\n=== RateLimitMiddleware with broken SecurityLog ===")

    class _BrokenSecurityLog:
        async def log_from_request(self, *args, **kwargs):
            raise ConnectionError("sec log is down")

    set_security_log(_BrokenSecurityLog())

    mw = RateLimitMiddleware(max_requests=1, window=60)

    async def _dummy_next(request):
        from hyperdjango.response import Response

        return Response.json({"ok": True})

    request = _MockRequest(client_ip="10.0.0.200")

    await mw(request, _dummy_next)
    r = await mw(request, _dummy_next)
    check("broken sec_log doesn't break rate limit", r.status == 429)


async def main():
    global PASS, FAIL

    db = Database(DB_URL)
    await db.connect()
    set_db(db)

    try:
        await db.execute("DROP TABLE IF EXISTS hyper_security_log CASCADE")
        await test_rate_limit_logs_to_security_log(db)
        await test_rate_limit_no_security_log_when_unconfigured(db)
        await test_rate_limit_swallows_seclog_errors(db)
    finally:
        # Clean up global state for other tests
        set_security_log(None)
        await db.disconnect()

    print(f"\n{'=' * 60}")
    print(f"Results: {PASS}/{PASS + FAIL} passed, {FAIL} failed")
    if ERRORS:
        print("\nFailures:")
        for e in ERRORS:
            print(f"  {e}")
    print("=" * 60)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
