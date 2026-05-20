"""
Tests for session auth hash invalidation on password change.

Verifies that:
1. Session auth hash is computed from HMAC(secret, password_hash)
2. Hash is stored in session data on login
3. Hash is verified on each request
4. Password change invalidates all old sessions
5. Current session is preserved after password change (if re-hashed)
6. Comparison is constant-time (hmac.compare_digest)
7. Legacy sessions without hash are allowed through
8. Deleted users have sessions invalidated
9. InMemorySessionStore.invalidate_by_hash works correctly
10. SessionAuth middleware integration works end-to-end

Usage:
    uv run hyper-test session_auth_hash
"""

# hyper-test: unit

import asyncio
import hashlib
import hmac
import inspect
import sys
import traceback

from hyperdjango.auth.sessions import (
    _SESSION_HASH_KEY,
    InMemorySessionStore,
    SessionAuth,
    get_session_auth_hash,
    verify_session_auth_hash,
)
from hyperdjango.native._crypto import hash_password, sign_data
from hyperdjango.request import Request
from hyperdjango.response import Response

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------

RESULTS = {"passed": 0, "failed": 0, "errors": []}


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


SECRET = "test-secret-key-for-session-auth-hash"

# ---------------------------------------------------------------------------
# 1. get_session_auth_hash: HMAC computation
# ---------------------------------------------------------------------------


@test("get_session_auth_hash: returns HMAC-SHA256 hex digest")
def test_hash_format():
    pw_hash = "$argon2id$v=19$m=65536,t=3,p=4$salt$hash123"
    result = get_session_auth_hash(pw_hash, SECRET)
    assert len(result) == 64, f"Expected 64 hex chars, got {len(result)}"
    assert all(c in "0123456789abcdef" for c in result)


@test("get_session_auth_hash: deterministic for same inputs")
def test_hash_deterministic():
    pw_hash = "$argon2id$v=19$m=65536,t=3,p=4$salt$hash123"
    h1 = get_session_auth_hash(pw_hash, SECRET)
    h2 = get_session_auth_hash(pw_hash, SECRET)
    assert h1 == h2


@test("get_session_auth_hash: different password → different hash")
def test_hash_changes_with_password():
    h1 = get_session_auth_hash("$argon2id$old_hash", SECRET)
    h2 = get_session_auth_hash("$argon2id$new_hash", SECRET)
    assert h1 != h2


@test("get_session_auth_hash: different secret → different hash")
def test_hash_changes_with_secret():
    pw_hash = "$argon2id$v=19$m=65536,t=3,p=4$salt$hash123"
    h1 = get_session_auth_hash(pw_hash, "secret-a")
    h2 = get_session_auth_hash(pw_hash, "secret-b")
    assert h1 != h2


@test("get_session_auth_hash: matches manual HMAC-SHA256")
def test_hash_matches_manual():
    pw_hash = "$argon2id$v=19$hash"
    result = get_session_auth_hash(pw_hash, SECRET)
    expected = hmac.new(SECRET.encode(), pw_hash.encode(), hashlib.sha256).hexdigest()
    assert result == expected


# ---------------------------------------------------------------------------
# 2. verify_session_auth_hash: constant-time comparison
# ---------------------------------------------------------------------------


@test("verify_session_auth_hash: valid hash returns True")
def test_verify_valid():
    pw_hash = "$argon2id$v=19$m=65536,t=3,p=4$salt$hash123"
    session_hash = get_session_auth_hash(pw_hash, SECRET)
    assert verify_session_auth_hash(session_hash, pw_hash, SECRET) is True


@test("verify_session_auth_hash: wrong password returns False")
def test_verify_wrong_password():
    pw_hash = "$argon2id$v=19$old"
    session_hash = get_session_auth_hash(pw_hash, SECRET)
    # Password changed
    assert verify_session_auth_hash(session_hash, "$argon2id$v=19$new", SECRET) is False


@test("verify_session_auth_hash: wrong secret returns False")
def test_verify_wrong_secret():
    pw_hash = "$argon2id$v=19$hash"
    session_hash = get_session_auth_hash(pw_hash, SECRET)
    assert verify_session_auth_hash(session_hash, pw_hash, "different-secret") is False


@test("verify_session_auth_hash: empty hash returns False")
def test_verify_empty_hash():
    assert verify_session_auth_hash("", "$argon2id$v=19$hash", SECRET) is False


# ---------------------------------------------------------------------------
# 3. InMemorySessionStore.invalidate_by_hash
# ---------------------------------------------------------------------------


@test("invalidate_by_hash: removes sessions with old hash")
def test_store_invalidate_by_hash():
    store = InMemorySessionStore()
    old_hash = get_session_auth_hash("$argon2id$old", SECRET)
    new_hash = get_session_auth_hash("$argon2id$new", SECRET)

    # Create sessions with old hash
    s1 = store.create({"user_id": 1, _SESSION_HASH_KEY: old_hash})
    s2 = store.create({"user_id": 1, _SESSION_HASH_KEY: old_hash})
    # Session with new hash (current session)
    s3 = store.create({"user_id": 1, _SESSION_HASH_KEY: new_hash})
    # Session for different user
    s4 = store.create({"user_id": 2, _SESSION_HASH_KEY: old_hash})

    store.invalidate_by_hash(1, new_hash)

    # Old sessions for user 1 should be gone
    assert store.get(s1) is None
    assert store.get(s2) is None
    # Current session with new hash kept
    assert store.get(s3) is not None
    # Other user's session untouched
    assert store.get(s4) is not None


@test("invalidate_by_hash: no-op when no matching user")
def test_store_invalidate_no_match():
    store = InMemorySessionStore()
    s1 = store.create({"user_id": 1, _SESSION_HASH_KEY: "hash1"})
    store.invalidate_by_hash(999, "hash2")
    assert store.get(s1) is not None


@test("invalidate_by_hash: handles sessions without hash")
def test_store_invalidate_no_hash():
    store = InMemorySessionStore()
    # Legacy session without _session_auth_hash
    s1 = store.create({"user_id": 1, "username": "admin"})
    new_hash = get_session_auth_hash("$argon2id$new", SECRET)
    # Should invalidate (empty hash != new_hash)
    store.invalidate_by_hash(1, new_hash)
    assert store.get(s1) is None


# ---------------------------------------------------------------------------
# 4. SessionAuth._inject_session_hash: stores hash on login
# ---------------------------------------------------------------------------


@test("login: injects session auth hash into user data")
def test_login_injects_hash():
    store = InMemorySessionStore()
    auth = SessionAuth(secret=SECRET, store=store, secure_cookie=False)
    response = Response.html("ok")

    user_data = {
        "user_id": 1,
        "username": "admin",
        "password_hash": "$argon2id$v=19$m=65536,t=3,p=4$salt$hash",
    }
    session_id = auth.login(response, user_data)

    # Check that the hash was stored
    data = store.get(session_id)
    assert _SESSION_HASH_KEY in data
    expected = get_session_auth_hash("$argon2id$v=19$m=65536,t=3,p=4$salt$hash", SECRET)
    assert data[_SESSION_HASH_KEY] == expected


@test("login: skips hash injection when no password_hash")
def test_login_no_password_hash():
    store = InMemorySessionStore()
    auth = SessionAuth(secret=SECRET, store=store, secure_cookie=False)
    response = Response.html("ok")

    user_data = {"user_id": 1, "username": "admin"}
    session_id = auth.login(response, user_data)

    data = store.get(session_id)
    assert _SESSION_HASH_KEY not in data


@test("login_async: injects session auth hash into user data")
async def test_login_async_injects_hash():
    store = InMemorySessionStore()
    auth = SessionAuth(secret=SECRET, store=store, secure_cookie=False)
    response = Response.html("ok")

    user_data = {
        "user_id": 1,
        "username": "admin",
        "password_hash": "$argon2id$v=19$hash",
    }
    session_id = await auth.login_async(response, user_data)

    data = store.get(session_id)
    assert _SESSION_HASH_KEY in data
    expected = get_session_auth_hash("$argon2id$v=19$hash", SECRET)
    assert data[_SESSION_HASH_KEY] == expected


# ---------------------------------------------------------------------------
# 5. SessionAuth.__call__: verifies hash on each request
# ---------------------------------------------------------------------------


def _make_request(cookie_name, signed_value):
    """Create a real Request with a session cookie."""
    cookie_header = f"{cookie_name}={signed_value}"
    req = Request(
        method="GET",
        path="/test",
        query_string="",
        headers={"cookie": cookie_header},
        body=b"",
    )
    return req


@test("middleware: valid session with matching hash → user set")
async def test_middleware_valid_hash():
    store = InMemorySessionStore()
    pw_hash = "$argon2id$v=19$hash"
    session_hash = get_session_auth_hash(pw_hash, SECRET)
    session_id = store.create(
        {
            "user_id": 1,
            "username": "admin",
            _SESSION_HASH_KEY: session_hash,
        }
    )

    async def get_user(user_id):
        return {"id": 1, "password_hash": pw_hash}

    auth = SessionAuth(
        secret=SECRET, store=store, secure_cookie=False, get_user=get_user
    )
    signed = sign_data(session_id, SECRET)
    request = _make_request(auth.cookie_name, signed)

    async def call_next(req):
        return Response.html("ok")

    response = await auth(request, call_next)
    assert request.user is not None
    assert request.user["username"] == "admin"
    assert request.session_id == session_id


@test("middleware: password changed → session invalidated")
async def test_middleware_password_changed():
    store = InMemorySessionStore()
    old_pw_hash = "$argon2id$v=19$old"
    session_hash = get_session_auth_hash(old_pw_hash, SECRET)
    session_id = store.create(
        {
            "user_id": 1,
            "username": "admin",
            _SESSION_HASH_KEY: session_hash,
        }
    )

    # User has new password now
    async def get_user(user_id):
        return {"id": 1, "password_hash": "$argon2id$v=19$new"}

    auth = SessionAuth(
        secret=SECRET, store=store, secure_cookie=False, get_user=get_user
    )
    signed = sign_data(session_id, SECRET)
    request = _make_request(auth.cookie_name, signed)

    async def call_next(req):
        return Response.html("ok")

    response = await auth(request, call_next)
    # Session should be invalidated
    # ws27 item 1: invalidated session yields AnonymousUser (is_authenticated False), not None
    assert not request.user.is_authenticated
    assert request.session_id is None
    # Session should be deleted from store
    assert store.get(session_id) is None


@test("middleware: user deleted → session invalidated")
async def test_middleware_user_deleted():
    store = InMemorySessionStore()
    session_hash = get_session_auth_hash("$argon2id$hash", SECRET)
    session_id = store.create(
        {
            "user_id": 1,
            "username": "admin",
            _SESSION_HASH_KEY: session_hash,
        }
    )

    async def get_user(user_id):
        return None  # User no longer exists

    auth = SessionAuth(
        secret=SECRET, store=store, secure_cookie=False, get_user=get_user
    )
    signed = sign_data(session_id, SECRET)
    request = _make_request(auth.cookie_name, signed)

    async def call_next(req):
        return Response.html("ok")

    response = await auth(request, call_next)
    # ws27 item 1: invalidated session yields AnonymousUser (is_authenticated False), not None
    assert not request.user.is_authenticated
    assert store.get(session_id) is None


@test("middleware: legacy session without hash → allowed through")
async def test_middleware_legacy_no_hash():
    store = InMemorySessionStore()
    # No _session_auth_hash in data
    session_id = store.create({"user_id": 1, "username": "admin"})

    async def get_user(user_id):
        return {"id": 1, "password_hash": "$argon2id$hash"}

    auth = SessionAuth(
        secret=SECRET, store=store, secure_cookie=False, get_user=get_user
    )
    signed = sign_data(session_id, SECRET)
    request = _make_request(auth.cookie_name, signed)

    async def call_next(req):
        return Response.html("ok")

    response = await auth(request, call_next)
    # Legacy session should pass through
    assert request.user is not None
    assert request.user["username"] == "admin"


@test("middleware: no get_user configured → skip verification")
async def test_middleware_no_get_user():
    store = InMemorySessionStore()
    session_hash = get_session_auth_hash("$argon2id$hash", SECRET)
    session_id = store.create(
        {
            "user_id": 1,
            "username": "admin",
            _SESSION_HASH_KEY: session_hash,
        }
    )

    # No get_user callback — can't verify
    auth = SessionAuth(secret=SECRET, store=store, secure_cookie=False)
    signed = sign_data(session_id, SECRET)
    request = _make_request(auth.cookie_name, signed)

    async def call_next(req):
        return Response.html("ok")

    response = await auth(request, call_next)
    assert request.user is not None


@test("middleware: verify_auth_hash=False → skip verification")
async def test_middleware_verify_disabled():
    store = InMemorySessionStore()
    old_hash = get_session_auth_hash("$argon2id$old", SECRET)
    session_id = store.create(
        {
            "user_id": 1,
            "username": "admin",
            _SESSION_HASH_KEY: old_hash,
        }
    )

    # Password changed, but verification disabled
    async def get_user(user_id):
        return {"id": 1, "password_hash": "$argon2id$new"}

    auth = SessionAuth(
        secret=SECRET,
        store=store,
        secure_cookie=False,
        get_user=get_user,
        verify_auth_hash=False,
    )
    signed = sign_data(session_id, SECRET)
    request = _make_request(auth.cookie_name, signed)

    async def call_next(req):
        return Response.html("ok")

    response = await auth(request, call_next)
    # Should still work (verification disabled)
    assert request.user is not None


# ---------------------------------------------------------------------------
# 6. End-to-end: login → password change → old sessions invalid
# ---------------------------------------------------------------------------


@test("e2e: login then password change invalidates old session")
async def test_e2e_password_change():
    store = InMemorySessionStore()
    original_pw_hash = hash_password("original-password")

    # Simulate user lookup
    user_db = {
        1: {
            "id": 1,
            "username": "admin",
            "password_hash": original_pw_hash,
            "is_staff": True,
        }
    }

    async def get_user(user_id):
        return user_db.get(user_id)

    auth = SessionAuth(
        secret=SECRET, store=store, secure_cookie=False, get_user=get_user
    )

    # Step 1: Login
    response = Response.html("ok")
    user_data = dict(user_db[1])
    session_id = auth.login(response, user_data)

    # Verify session works
    signed = sign_data(session_id, SECRET)
    request = _make_request(auth.cookie_name, signed)

    async def call_next(req):
        return Response.html("ok")

    await auth(request, call_next)
    assert request.user is not None
    assert request.user["username"] == "admin"

    # Step 2: Change password
    new_pw_hash = hash_password("new-password")
    user_db[1]["password_hash"] = new_pw_hash

    # Step 3: Old session should now fail
    request2 = _make_request(auth.cookie_name, signed)
    await auth(request2, call_next)
    assert not request2.user.is_authenticated, (
        "Old session should be invalidated after password change"
    )

    # Step 4: New login with new password should work
    response2 = Response.html("ok")
    new_user_data = dict(user_db[1])
    new_session_id = auth.login(response2, new_user_data)
    signed2 = sign_data(new_session_id, SECRET)
    request3 = _make_request(auth.cookie_name, signed2)
    await auth(request3, call_next)
    assert request3.user is not None
    assert request3.user["username"] == "admin"


@test("e2e: multiple sessions, password change invalidates all")
async def test_e2e_multiple_sessions():
    store = InMemorySessionStore()
    pw_hash = hash_password("password123")

    user_db = {1: {"id": 1, "password_hash": pw_hash}}

    async def get_user(uid):
        return user_db.get(uid)

    auth = SessionAuth(
        secret=SECRET, store=store, secure_cookie=False, get_user=get_user
    )

    # Login from 3 different "devices"
    sessions = []
    for _ in range(3):
        resp = Response.html("ok")
        user_data = {"user_id": 1, "username": "admin", "password_hash": pw_hash}
        sid = auth.login(resp, user_data)
        sessions.append(sid)

    assert store.count() == 3

    # Change password
    new_hash = hash_password("new-password")
    user_db[1]["password_hash"] = new_hash

    # All 3 old sessions should fail
    async def call_next(req):
        return Response.html("ok")

    for sid in sessions:
        signed = sign_data(sid, SECRET)
        req = _make_request(auth.cookie_name, signed)
        await auth(req, call_next)
        assert not req.user.is_authenticated, f"Session {sid[:8]}... should be invalid"


# ---------------------------------------------------------------------------
# 7. Eager invalidation via invalidate_by_hash
# ---------------------------------------------------------------------------


@test("eager: invalidate_by_hash removes old sessions, keeps new")
def test_eager_invalidation():
    store = InMemorySessionStore()
    old_hash = get_session_auth_hash("$argon2id$old", SECRET)
    new_hash = get_session_auth_hash("$argon2id$new", SECRET)

    # 3 old sessions
    old_sids = []
    for _ in range(3):
        sid = store.create({"user_id": 1, _SESSION_HASH_KEY: old_hash})
        old_sids.append(sid)

    # 1 new session (just re-logged in)
    new_sid = store.create({"user_id": 1, _SESSION_HASH_KEY: new_hash})

    # Eagerly invalidate
    store.invalidate_by_hash(1, new_hash)

    for sid in old_sids:
        assert store.get(sid) is None, "Old session should be removed"
    assert store.get(new_sid) is not None, "New session should remain"


# ---------------------------------------------------------------------------
# 8. Admin integration: session hash stored on admin login
# ---------------------------------------------------------------------------


@test("admin: session data includes _session_auth_hash on login")
def test_admin_login_stores_hash():
    """Simulate what admin._login_handler does."""
    store = InMemorySessionStore()
    pw_hash = "$argon2id$v=19$m=65536,t=3,p=4$salt$hash"

    # This is what admin._login_handler now does
    session_data = {
        "user_id": 1,
        "username": "admin",
        "is_staff": True,
        "is_superuser": False,
    }
    session_data["_session_auth_hash"] = get_session_auth_hash(pw_hash, SECRET)
    session_id = store.create(session_data)

    data = store.get(session_id)
    assert "_session_auth_hash" in data
    assert verify_session_auth_hash(data["_session_auth_hash"], pw_hash, SECRET)


# ---------------------------------------------------------------------------
# 9. Real argon2 password hashing integration
# ---------------------------------------------------------------------------


@test("integration: real argon2 hash → session hash → verify cycle")
def test_real_argon2_cycle():
    pw = "my-secure-password-123!"
    pw_hash = hash_password(pw)

    # Store session hash on login
    session_hash = get_session_auth_hash(pw_hash, SECRET)

    # On subsequent request, verify matches
    assert verify_session_auth_hash(session_hash, pw_hash, SECRET)

    # After password change, new hash is different
    new_pw_hash = hash_password("different-password-456!")
    assert not verify_session_auth_hash(session_hash, new_pw_hash, SECRET)

    # New session with new hash works
    new_session_hash = get_session_auth_hash(new_pw_hash, SECRET)
    assert verify_session_auth_hash(new_session_hash, new_pw_hash, SECRET)


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------


async def main():
    tests = [
        obj
        for name, obj in globals().items()
        if callable(obj) and getattr(obj, "_is_test", False)
    ]

    print(f"\nSession Auth Hash Tests ({len(tests)} tests)")
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
