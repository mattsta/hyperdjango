"""
Tests for production security hardening.

Tests SecurityHeadersMiddleware enhancements, SecurityLog event audit,
and SecurityEvent types.

Usage:
    uv run hyper-test security
"""

# hyper-test: db_isolated

import asyncio
import inspect
import os
import sys
import traceback

from hyperdjango.database import Database, get_db, set_db
from hyperdjango.security import (
    SecurityEvent,
    SecurityLog,
    get_security_log,
    set_security_log,
)
from hyperdjango.standalone_middleware import SecurityHeadersMiddleware

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------

RESULTS = {"passed": 0, "failed": 0, "errors": []}
DB_URL = os.environ.get("DATABASE_URL", "postgres://localhost/hyperdjango_test")


def test(name):
    def decorator(func):
        async def wrapper():
            try:
                if inspect.iscoroutinefunction(func):
                    await func()
                else:
                    func()
                RESULTS["passed"] += 1
                print(f"  ✓ {name}")
            except Exception as e:
                RESULTS["failed"] += 1
                RESULTS["errors"].append((name, traceback.format_exc()))
                print(f"  ✗ {name}: {e}")

        wrapper.__name__ = name
        wrapper._is_test = True
        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# DB setup / teardown
# ---------------------------------------------------------------------------


async def setup_db():
    db = Database(DB_URL)
    await db.connect()
    set_db(db)
    return db


async def teardown_db(db):
    await db.execute("DROP TABLE IF EXISTS hyper_security_log CASCADE")
    await db.disconnect()


# ---------------------------------------------------------------------------
# Unit Tests: SecurityHeadersMiddleware
# ---------------------------------------------------------------------------


@test("SecurityHeaders: default includes referrer-policy")
def test_headers_referrer_policy():
    mw = SecurityHeadersMiddleware()
    assert "referrer-policy" in mw._headers
    assert mw._headers["referrer-policy"] == "same-origin"


@test("SecurityHeaders: default includes COOP")
def test_headers_coop():
    mw = SecurityHeadersMiddleware()
    assert "cross-origin-opener-policy" in mw._headers
    assert mw._headers["cross-origin-opener-policy"] == "same-origin"


@test("SecurityHeaders: permissions-policy configurable")
def test_headers_permissions_policy():
    mw = SecurityHeadersMiddleware(
        permissions_policy="camera=(), microphone=(), geolocation=()"
    )
    assert "permissions-policy" in mw._headers
    assert "camera=()" in mw._headers["permissions-policy"]


@test("SecurityHeaders: permissions-policy absent when None")
def test_headers_permissions_policy_none():
    mw = SecurityHeadersMiddleware()
    assert "permissions-policy" not in mw._headers


@test("SecurityHeaders: all headers present with full config")
def test_headers_full_config():
    mw = SecurityHeadersMiddleware(
        hsts=True,
        csp="default-src 'self'",
        referrer_policy="no-referrer",
        permissions_policy="camera=()",
        cross_origin_opener_policy="same-origin-allow-popups",
    )
    assert "strict-transport-security" in mw._headers
    assert "content-security-policy" in mw._headers
    assert "referrer-policy" in mw._headers
    assert "permissions-policy" in mw._headers
    assert "cross-origin-opener-policy" in mw._headers
    assert "x-content-type-options" in mw._headers
    assert "x-frame-options" in mw._headers
    assert mw._headers["referrer-policy"] == "no-referrer"


@test("SecurityHeaders: custom referrer-policy")
def test_headers_custom_referrer():
    mw = SecurityHeadersMiddleware(referrer_policy="no-referrer")
    assert mw._headers["referrer-policy"] == "no-referrer"


@test("SecurityHeaders: disable referrer-policy")
def test_headers_disable_referrer():
    mw = SecurityHeadersMiddleware(referrer_policy="")
    assert "referrer-policy" not in mw._headers


# ---------------------------------------------------------------------------
# Host-header validation (anti header-injection / cache-poisoning)
# ---------------------------------------------------------------------------


class _HostReq:
    def __init__(self, host):
        self.headers = {"host": host}
        self.path = "/reset"
        self.scope = {"scheme": "http"}
        self.cookies = {}

    @property
    def host(self):
        return self.headers.get("host", "")


@test("SecurityHeaders: forged Host rejected (400) when ALLOWED_HOSTS is set")
async def test_host_header_validation():
    from hyperdjango.response import Response

    async def app(req):
        # A view building an absolute URL from request.host — the danger.
        return Response.text(f"https://{req.host}/reset")

    mw = SecurityHeadersMiddleware()
    mw._allowed_hosts = ("example.com",)

    async def status_for(host):
        r = await mw(_HostReq(host), app)
        return getattr(r, "status_code", getattr(r, "status", None))

    assert await status_for("example.com") == 200, "allowed host must pass"
    assert await status_for("example.com:8000") == 200, "port must be stripped"
    assert await status_for("attacker.com") == 400, "forged Host must be 400"
    assert await status_for("") == 400, "missing Host must be 400 when allowlist set"
    assert await status_for("sub.example.com") == 400, (
        "non-listed subdomain must be 400"
    )

    # Dev default (no allowlist) stays open so localhost just works.
    mw_dev = SecurityHeadersMiddleware()
    mw_dev._allowed_hosts = ()
    r = await mw_dev(_HostReq("anything.local"), app)
    assert getattr(r, "status_code", getattr(r, "status", None)) == 200


# ---------------------------------------------------------------------------
# Unit Tests: SecurityEvent enum
# ---------------------------------------------------------------------------


@test("SecurityEvent: all event types defined")
def test_security_event_types():
    assert SecurityEvent.LOGIN_SUCCESS.value == "login_success"
    assert SecurityEvent.LOGIN_FAILED.value == "login_failed"
    assert SecurityEvent.PERMISSION_DENIED.value == "permission_denied"
    assert SecurityEvent.CSRF_VIOLATION.value == "csrf_violation"
    assert SecurityEvent.RATE_LIMIT_HIT.value == "rate_limit_hit"
    assert SecurityEvent.SESSION_CREATED.value == "session_created"
    assert SecurityEvent.SUSPICIOUS_INPUT.value == "suspicious_input"
    assert SecurityEvent.PATH_TRAVERSAL_ATTEMPT.value == "path_traversal_attempt"
    assert len(SecurityEvent) >= 15


# ---------------------------------------------------------------------------
# DB Tests: SecurityLog
# ---------------------------------------------------------------------------


@test("DB: SecurityLog ensure_table creates table")
async def test_seclog_ensure_table():
    db = get_db()
    log = SecurityLog(db)
    await log.ensure_table()
    # Verify table exists
    result = await db.query_val(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_name = 'hyper_security_log'"
    )
    assert result >= 1


@test("DB: SecurityLog log and retrieve event")
async def test_seclog_log_event():
    db = get_db()
    log = SecurityLog(db)
    await db.execute("DELETE FROM hyper_security_log")

    await log.log(
        SecurityEvent.LOGIN_SUCCESS,
        user_id=42,
        ip="192.168.1.1",
        detail="Logged in via password",
    )

    events = await log.get_recent(limit=10)
    assert len(events) == 1
    assert events[0]["event"] == "login_success"
    assert events[0]["user_id"] == 42
    assert events[0]["ip_address"] == "192.168.1.1"
    assert events[0]["detail"] == "Logged in via password"
    assert events[0]["timestamp"] is not None


@test("DB: SecurityLog batching buffers then flushes as one INSERT")
async def test_seclog_batching():
    db = get_db()
    log = SecurityLog(db, batch_size=3)
    await db.execute("DELETE FROM hyper_security_log")

    # First two are buffered — nothing written yet.
    await log.log(SecurityEvent.LOGIN_FAILED, ip="9.9.9.1")
    await log.log(SecurityEvent.LOGIN_FAILED, ip="9.9.9.2")
    assert len(await log.get_recent(limit=10)) == 0, (
        "buffered events must not be written yet"
    )

    # Third fills the batch → single multi-row INSERT of all three.
    await log.log(SecurityEvent.LOGIN_FAILED, ip="9.9.9.3")
    events = await log.get_recent(limit=10)
    assert len(events) == 3, f"expected 3 flushed events, got {len(events)}"

    # A partial batch is written by an explicit flush().
    await log.log(SecurityEvent.LOGIN_FAILED, ip="9.9.9.4")
    assert len(await log.get_recent(limit=10)) == 3
    await log.flush()
    assert len(await log.get_recent(limit=10)) == 4
    # flush() on an empty buffer is a no-op.
    await log.flush()
    assert len(await log.get_recent(limit=10)) == 4


@test("DB: SecurityLog get_for_user")
async def test_seclog_get_for_user():
    db = get_db()
    log = SecurityLog(db)
    await db.execute("DELETE FROM hyper_security_log")

    await log.log(SecurityEvent.LOGIN_SUCCESS, user_id=1, ip="1.1.1.1")
    await log.log(SecurityEvent.LOGIN_FAILED, user_id=2, ip="2.2.2.2")
    await log.log(SecurityEvent.PASSWORD_CHANGED, user_id=1, ip="1.1.1.1")

    user1_events = await log.get_for_user(1)
    assert len(user1_events) == 2
    assert all(e["user_id"] == 1 for e in user1_events)


@test("DB: SecurityLog get_for_ip")
async def test_seclog_get_for_ip():
    db = get_db()
    log = SecurityLog(db)
    await db.execute("DELETE FROM hyper_security_log")

    await log.log(SecurityEvent.LOGIN_FAILED, ip="10.0.0.1")
    await log.log(SecurityEvent.LOGIN_FAILED, ip="10.0.0.1")
    await log.log(SecurityEvent.LOGIN_SUCCESS, ip="10.0.0.2")

    ip_events = await log.get_for_ip("10.0.0.1")
    assert len(ip_events) == 2


@test("DB: SecurityLog get_by_event")
async def test_seclog_get_by_event():
    db = get_db()
    log = SecurityLog(db)
    await db.execute("DELETE FROM hyper_security_log")

    await log.log(SecurityEvent.LOGIN_FAILED, ip="1.1.1.1")
    await log.log(SecurityEvent.LOGIN_FAILED, ip="2.2.2.2")
    await log.log(SecurityEvent.LOGIN_SUCCESS, ip="3.3.3.3")
    await log.log(SecurityEvent.CSRF_VIOLATION, ip="4.4.4.4")

    failed = await log.get_by_event(SecurityEvent.LOGIN_FAILED, since_hours=1)
    assert len(failed) == 2


@test("DB: SecurityLog count_by_event")
async def test_seclog_count_by_event():
    db = get_db()
    log = SecurityLog(db)
    await db.execute("DELETE FROM hyper_security_log")

    for i in range(5):
        await log.log(SecurityEvent.RATE_LIMIT_HIT, ip=f"10.0.0.{i}")

    count = await log.count_by_event(SecurityEvent.RATE_LIMIT_HIT, since_hours=1)
    assert count == 5


@test("DB: SecurityLog count_by_ip")
async def test_seclog_count_by_ip():
    db = get_db()
    log = SecurityLog(db)
    await db.execute("DELETE FROM hyper_security_log")

    for _ in range(3):
        await log.log(SecurityEvent.LOGIN_FAILED, ip="10.0.0.99")
    await log.log(SecurityEvent.LOGIN_FAILED, ip="10.0.0.100")

    count = await log.count_by_ip(
        "10.0.0.99", SecurityEvent.LOGIN_FAILED, since_hours=1
    )
    assert count == 3


@test("DB: SecurityLog log_from_request")
async def test_seclog_log_from_request():
    db = get_db()
    log = SecurityLog(db)
    await db.execute("DELETE FROM hyper_security_log")

    # Fake request object
    class FakeRequest:
        client_ip = "192.168.0.1"
        headers = {"user-agent": "TestBrowser/1.0"}
        path = "/admin/login"
        user = None

    await log.log_from_request(
        SecurityEvent.LOGIN_FAILED,
        FakeRequest(),
        detail="Invalid password for user 'admin'",
    )

    events = await log.get_recent()
    assert len(events) == 1
    assert events[0]["ip_address"] == "192.168.0.1"
    assert events[0]["path"] == "/admin/login"
    assert events[0]["user_agent"] == "TestBrowser/1.0"
    assert "Invalid password" in events[0]["detail"]


@test("DB: SecurityLog log_from_request with user")
async def test_seclog_log_from_request_user():
    db = get_db()
    log = SecurityLog(db)
    await db.execute("DELETE FROM hyper_security_log")

    class FakeUser:
        id = 42

    class FakeRequest:
        client_ip = "10.0.0.1"
        headers = {}
        path = "/api/products"
        user = FakeUser()

    await log.log_from_request(
        SecurityEvent.PERMISSION_DENIED,
        FakeRequest(),
        detail="missing edit_product",
    )

    events = await log.get_recent()
    assert len(events) == 1
    assert events[0]["user_id"] == 42
    assert events[0]["event"] == "permission_denied"


@test("DB: SecurityLog cleanup old events")
async def test_seclog_cleanup():
    db = get_db()
    log = SecurityLog(db)
    await db.execute("DELETE FROM hyper_security_log")

    # Insert some events
    await log.log(SecurityEvent.LOGIN_SUCCESS, user_id=1)
    await log.log(SecurityEvent.LOGIN_SUCCESS, user_id=2)

    # Cleanup with 0 days = delete everything
    await log.cleanup(days=0)

    events = await log.get_recent()
    assert len(events) == 0


@test("DB: SecurityLog global singleton")
async def test_seclog_singleton():
    db = get_db()
    log = SecurityLog(db)
    set_security_log(log)
    assert get_security_log() is log
    set_security_log(None)


@test("DB: SecurityLog multiple event types in single query")
async def test_seclog_mixed_events():
    db = get_db()
    log = SecurityLog(db)
    await db.execute("DELETE FROM hyper_security_log")

    await log.log(SecurityEvent.LOGIN_SUCCESS, user_id=1, ip="1.1.1.1")
    await log.log(SecurityEvent.LOGIN_FAILED, ip="2.2.2.2", detail="bad password")
    await log.log(SecurityEvent.CSRF_VIOLATION, ip="3.3.3.3", path="/api/transfer")
    await log.log(SecurityEvent.RATE_LIMIT_HIT, ip="4.4.4.4", detail="100/min")
    await log.log(SecurityEvent.PERMISSION_DENIED, user_id=5, detail="no admin access")

    events = await log.get_recent()
    assert len(events) == 5
    event_types = {e["event"] for e in events}
    assert "login_success" in event_types
    assert "csrf_violation" in event_types
    assert "rate_limit_hit" in event_types


@test("DB: SecurityLog stores user_agent and path")
async def test_seclog_full_fields():
    db = get_db()
    log = SecurityLog(db)
    await db.execute("DELETE FROM hyper_security_log")

    await log.log(
        SecurityEvent.SUSPICIOUS_INPUT,
        ip="10.0.0.1",
        user_agent="SuspiciousBot/1.0",
        path="/search?q=<script>alert(1)</script>",
        detail="XSS attempt in search query",
    )

    events = await log.get_recent()
    assert len(events) == 1
    assert events[0]["user_agent"] == "SuspiciousBot/1.0"
    assert "<script>" in events[0]["path"]
    assert "XSS attempt" in events[0]["detail"]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def main():
    all_tests = []
    for name, obj in list(globals().items()):
        if callable(obj) and getattr(obj, "_is_test", False):
            all_tests.append(obj)

    unit_tests = [t for t in all_tests if not t.__name__.startswith("DB:")]
    db_tests = [t for t in all_tests if t.__name__.startswith("DB:")]

    print("\n═══ Unit Tests ═══")
    for t in unit_tests:
        await t()

    print("\n═══ DB Integration Tests ═══")
    try:
        db = await setup_db()
        try:
            log = SecurityLog(db)
            await log.ensure_table()
            for t in db_tests:
                await t()
        finally:
            await teardown_db(db)
    except Exception as e:
        print(f"\n  ⚠ Database connection failed ({e}), skipping integration tests")

    total = RESULTS["passed"] + RESULTS["failed"]
    print(f"\n{'═' * 60}")
    print(f"Results: {RESULTS['passed']}/{total} passed, {RESULTS['failed']} failed")
    if RESULTS["errors"]:
        print("\nFailures:")
        for name, tb in RESULTS["errors"]:
            print(f"\n--- {name} ---")
            print(tb)

    return RESULTS["failed"] == 0


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
