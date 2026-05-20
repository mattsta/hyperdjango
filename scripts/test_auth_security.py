"""
Regression tests for auth security hardening fixes.

Tests:
1. Timing attack prevention (dummy hash on missing user)
2. Session cookie secure flag (configurable)
3. Password reset HMAC uses full hash (not truncated)
4. Session parameterized INTERVAL (already tested in test_platform_security.py)

Usage:
    uv run hyper-test auth_security
"""

# hyper-test: db_isolated

import asyncio
import contextlib
import inspect
import os
import sys
import time
import traceback

from hyperdjango.database import Database, set_db

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
                print(f"  \u2713 {name}")
            except Exception as e:
                RESULTS["failed"] += 1
                RESULTS["errors"].append((name, traceback.format_exc()))
                print(f"  \u2717 {name}: {e}")

        wrapper.__name__ = name
        wrapper._is_test = True
        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Timing attack prevention
# ---------------------------------------------------------------------------


@test("authenticate: missing user takes similar time to wrong password")
async def test_timing_attack_prevention():
    db = Database(DB_URL)
    await db.connect()
    set_db(db)

    from hyperdjango.auth.passwords import hash_password
    from hyperdjango.auth.permissions import PermissionChecker
    from hyperdjango.auth.user import ensure_rbac_tables

    await ensure_rbac_tables(db)

    # Create a test user
    pw_hash = hash_password("correct-password")
    with contextlib.suppress(Exception):
        await db.execute(
            "INSERT INTO hyper_users (username, email, password_hash, is_active, is_staff) "
            "VALUES ($1, $2, $3, $4, $5)",
            "timing_test_user",
            "timing@test.com",
            pw_hash,
            True,
            False,
        )

    checker = PermissionChecker(db)

    # Time: existing user, wrong password (runs argon2 verify)
    t1 = time.monotonic()
    result1 = await checker.authenticate("timing_test_user", "wrong-password")
    wrong_pw_time = time.monotonic() - t1

    # Time: non-existent user (should run dummy verify for timing normalization)
    t2 = time.monotonic()
    result2 = await checker.authenticate("nonexistent_user_xyz", "any-password")
    missing_user_time = time.monotonic() - t2

    assert result1 is None
    assert result2 is None

    # Both should take roughly similar time (within 5x of each other)
    # The dummy hash makes the missing-user path take ~same as wrong-password path
    ratio = missing_user_time / wrong_pw_time if wrong_pw_time > 0 else 1.0
    assert 0.1 < ratio < 10.0, (
        f"Timing attack: missing user took {missing_user_time:.3f}s, "
        f"wrong password took {wrong_pw_time:.3f}s (ratio: {ratio:.2f}x)"
    )

    await db.execute("DELETE FROM hyper_users WHERE username = 'timing_test_user'")
    await db.disconnect()


# ---------------------------------------------------------------------------
# Session cookie secure flag
# ---------------------------------------------------------------------------


@test("SessionAuth: secure_cookie=True by default")
def test_session_secure_default():
    from hyperdjango.auth.sessions import SessionAuth

    sa = SessionAuth(secret="test")
    assert sa.secure_cookie is True


@test("SessionAuth: secure_cookie=False when explicitly set")
def test_session_secure_false():
    from hyperdjango.auth.sessions import SessionAuth

    sa = SessionAuth(secret="test", secure_cookie=False)
    assert sa.secure_cookie is False


@test("SessionAuth: login sets Secure flag based on config")
def test_session_login_secure_flag():
    from hyperdjango.auth.sessions import InMemorySessionStore, SessionAuth
    from hyperdjango.response import Response

    store = InMemorySessionStore(max_age=3600)

    # With secure=True
    sa_secure = SessionAuth(secret="test-secret", store=store, secure_cookie=True)
    resp1 = Response.html("<h1>OK</h1>")
    sa_secure.login(resp1, {"user_id": 1, "username": "test"})
    cookie1 = resp1.headers.get("set-cookie", "")
    assert "Secure" in cookie1, f"Missing Secure flag in cookie: {cookie1}"

    # With secure=False
    sa_insecure = SessionAuth(secret="test-secret", store=store, secure_cookie=False)
    resp2 = Response.html("<h1>OK</h1>")
    sa_insecure.login(resp2, {"user_id": 2, "username": "test2"})
    cookie2 = resp2.headers.get("set-cookie", "")
    assert "Secure" not in cookie2, f"Unexpected Secure flag in cookie: {cookie2}"


# ---------------------------------------------------------------------------
# Password reset HMAC full hash
# ---------------------------------------------------------------------------


@test("password reset: token uses full SHA-256 (64 hex chars)")
def test_password_reset_full_hash():
    from hyperdjango.auth.password_reset import PasswordResetTokenGenerator

    class FakeUser:
        id = 1
        password_hash = "fake_hash"
        last_login = None

    gen = PasswordResetTokenGenerator(secret_key="test-secret")
    token = gen.make_token(FakeUser())

    # Token format: {timestamp}-{signature}
    parts = token.split("-", 1)
    assert len(parts) == 2
    timestamp_str, signature = parts

    # Full SHA-256 hex = 64 chars (not truncated to 32)
    assert len(signature) == 64, (
        f"HMAC truncated: got {len(signature)} chars, expected 64"
    )

    # Verify token works
    assert gen.check_token(FakeUser(), token) is True


@test("password reset: old truncated tokens rejected")
def test_password_reset_truncated_rejected():
    from hyperdjango.auth.password_reset import PasswordResetTokenGenerator

    class FakeUser:
        id = 1
        password_hash = "fake_hash"
        last_login = None

    gen = PasswordResetTokenGenerator(secret_key="test-secret")
    token = gen.make_token(FakeUser())

    # Truncate the signature to 32 chars (old format)
    parts = token.split("-", 1)
    truncated = f"{parts[0]}-{parts[1][:32]}"

    # Truncated token should be rejected
    assert gen.check_token(FakeUser(), truncated) is False


# ---------------------------------------------------------------------------
# Dummy hash constant
# ---------------------------------------------------------------------------


@test("permissions: _DUMMY_HASH is pre-computed and valid argon2")
def test_dummy_hash_exists():
    from hyperdjango.auth.permissions import _DUMMY_HASH

    assert _DUMMY_HASH is not None
    assert isinstance(_DUMMY_HASH, str)
    assert _DUMMY_HASH.startswith("$argon2")


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------


async def main():
    tests = [
        obj
        for name, obj in globals().items()
        if callable(obj) and getattr(obj, "_is_test", False)
    ]

    print(f"\nAuth Security Regression Tests ({len(tests)} tests)")
    print("=" * 60)

    for t in tests:
        await t()

    print(f"\n{'=' * 60}")
    print(f"Results: {RESULTS['passed']} passed, {RESULTS['failed']} failed")

    if RESULTS["errors"]:
        print("\nFailures:")
        for name, tb in RESULTS["errors"]:
            print(f"\n--- {name} ---")
            print(tb)

    return 0 if RESULTS["failed"] == 0 else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
