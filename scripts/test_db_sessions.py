#!/usr/bin/env python3
"""Test database-backed session store with PostgreSQL UNLOGGED tables.

Tests:
1. Table creation (UNLOGGED)
2. Session CRUD (create, get, update, delete)
3. Session expiry (expired sessions return None)
4. Session cleanup (remove expired)
5. User session invalidation (log out everywhere)
6. Session hash invalidation (password change)
7. Multi-session per user
8. SessionAuth integration with DatabaseSessionStore
9. Touch (extend expiry without changing data)
10. InMemorySessionStore compatibility
11. SessionAuth async/sync store transparency

Run: uv run hyper-test db_sessions
Requires: PostgreSQL running, DATABASE_URL or default hyperdjango_test
"""

# hyper-test: db_isolated

import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta

from hyperdjango.auth.db_sessions import DatabaseSessionStore, HyperSession
from hyperdjango.auth.sessions import (
    InMemorySessionStore,
    SessionAuth,
)
from hyperdjango.database import Database, set_db
from hyperdjango.models import create_table_for_model
from hyperdjango.request import Request
from hyperdjango.response import Response

DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgres://localhost/hyperdjango_test",
)

passed = 0
failed = 0


def utc_naive(dt: datetime) -> datetime:
    """Flatten an aware datetime to naive UTC.

    ``hyper_sessions.expires_at`` is TIMESTAMPTZ but pg.zig hands the column
    back as a naive UTC datetime, so bounds computed from ``datetime.now(UTC)``
    have to be flattened the same way before they can be compared to it.
    """
    return dt.astimezone(UTC).replace(tzinfo=None)


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        print(f"  PASS: {name}")
        passed += 1
    else:
        print(f"  FAIL: {name} — {detail}")
        failed += 1


async def test_in_memory_store():
    """Test InMemorySessionStore basic operations."""
    print("\n=== InMemorySessionStore ===")

    store = InMemorySessionStore(max_age=5)

    # Create
    sid = store.create({"user_id": 1, "username": "alice"})
    check("create returns session ID", isinstance(sid, str) and len(sid) > 10)

    # Get
    data = store.get(sid)
    check("get returns data", data is not None)
    check("data has user_id", data.get("user_id") == 1)
    check("data has username", data.get("username") == "alice")

    # Update
    store.update(sid, {"user_id": 1, "username": "alice", "role": "admin"})
    data = store.get(sid)
    check("update preserves data", data.get("role") == "admin")

    # Delete
    store.delete(sid)
    check("delete removes session", store.get(sid) is None)

    # Count
    store.create({"user_id": 1})
    store.create({"user_id": 2})
    check("count returns 2", store.count() == 2)

    # Invalidate for user
    store.create({"user_id": 1})  # second session for user 1
    store.invalidate_for_user(1)
    check("invalidate_for_user removes user sessions", store.count() == 1)


async def test_db_store_crud(db):
    """Test DatabaseSessionStore CRUD operations."""
    print("\n=== DatabaseSessionStore CRUD ===")

    store = DatabaseSessionStore(max_age=3600)

    # Clean slate
    await HyperSession.objects.delete()

    # Create
    sid = await store.create({"user_id": 42, "username": "bob"})
    check("create returns session ID", isinstance(sid, str) and len(sid) > 10)

    # Get
    data = await store.get(sid)
    check("get returns data", data is not None)
    check("data has user_id", data.get("user_id") == 42)
    check("data has username", data.get("username") == "bob")

    # Update
    await store.update(sid, {"user_id": 42, "username": "bob", "role": "admin"})
    data = await store.get(sid)
    check("update preserves data", data is not None and data.get("role") == "admin")

    # Delete
    await store.delete(sid)
    data = await store.get(sid)
    check("delete removes session", data is None)

    # Count
    await store.create({"user_id": 1})
    await store.create({"user_id": 2})
    count = await store.count()
    check("count returns 2", count == 2, f"got {count}")


async def test_db_store_expiry(db):
    """Test session expiry behavior."""
    print("\n=== Session Expiry ===")

    store = DatabaseSessionStore(max_age=60)
    await HyperSession.objects.delete()

    before = datetime.now(UTC)
    sid = await store.create({"user_id": 99})
    after = datetime.now(UTC)
    data = await store.get(sid)
    check("session valid before expiry", data is not None)

    # Expiry is a stored `expires_at` compared against now, so the two halves
    # of the contract can each be checked exactly. (1) create() stamps the
    # column from max_age: it lands inside the window the call itself spanned.
    row = await HyperSession.objects.filter(session_id=sid).first()
    check(
        "create stamps expires_at from max_age",
        utc_naive(before) + timedelta(seconds=60)
        <= row.expires_at
        <= utc_naive(after) + timedelta(seconds=60),
        f"got {row.expires_at} for a call spanning {before}..{after}",
    )
    # (2) Once that instant has passed the session is gone. Moving the column
    # into the past is exactly what waiting out max_age would have produced —
    # and unlike a sleep it cannot under- or over-shoot on a loaded machine.
    await HyperSession.objects.filter(session_id=sid).update(
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    data = await store.get(sid)
    check("session expired after max_age", data is None)


async def test_db_store_cleanup(db):
    """Test cleanup of expired sessions."""
    print("\n=== Session Cleanup ===")

    store = DatabaseSessionStore(max_age=3600)
    await HyperSession.objects.delete()

    # Create sessions
    expired = [await store.create({"user_id": i}) for i in (1, 2, 3)]
    live = await store.create({"user_id": 4})

    total_before = await db.query_val("SELECT COUNT(*) FROM hyper_sessions")
    check("4 sessions created", total_before == 4)

    # Push three of them past their expiry by writing the column — the same
    # state waiting out a short max_age would reach, minus the wall clock (and
    # minus the risk that a loaded runner's oversleep expires the row that is
    # supposed to survive the sweep).
    await HyperSession.objects.filter(session_id__in=expired).update(
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )

    # Cleanup
    await store.cleanup()
    total_after = await db.query_val("SELECT COUNT(*) FROM hyper_sessions")
    check("cleanup removed expired sessions", total_after == 1, f"got {total_after}")
    check("cleanup kept the unexpired session", await store.get(live) is not None)


async def test_db_store_user_invalidation(db):
    """Test invalidating all sessions for a user."""
    print("\n=== User Session Invalidation ===")

    store = DatabaseSessionStore(max_age=3600)
    await HyperSession.objects.delete()

    # Create multiple sessions for same user
    sid1 = await store.create({"user_id": 10, "device": "laptop"})
    sid2 = await store.create({"user_id": 10, "device": "phone"})
    sid3 = await store.create({"user_id": 20, "device": "tablet"})

    count = await store.count()
    check("3 sessions exist", count == 3)

    # Invalidate user 10's sessions
    await store.invalidate_for_user(10)

    # User 10's sessions gone
    data1 = await store.get(sid1)
    data2 = await store.get(sid2)
    check("user 10 session 1 invalidated", data1 is None)
    check("user 10 session 2 invalidated", data2 is None)

    # User 20's session intact
    data3 = await store.get(sid3)
    check("user 20 session intact", data3 is not None)


async def test_db_store_hash_invalidation(db):
    """Test session hash invalidation (password change)."""
    print("\n=== Session Hash Invalidation ===")

    store = DatabaseSessionStore(max_age=3600)
    await HyperSession.objects.delete()

    # Create sessions with hash
    sid1 = await store.create(
        {
            "user_id": 5,
            "_session_auth_hash": "hash_v1",  # ws27 item 8: canonical key
        }
    )
    sid2 = await store.create(
        {
            "user_id": 5,
            "_session_auth_hash": "hash_v1",  # ws27 item 8: canonical key
        }
    )
    # New session with updated hash (after password change)
    sid3 = await store.create(
        {
            "user_id": 5,
            "_session_auth_hash": "hash_v2",  # ws27 item 8: canonical key
        }
    )

    # Invalidate old hash
    await store.invalidate_by_hash(5, "hash_v2")

    data1 = await store.get(sid1)
    data2 = await store.get(sid2)
    data3 = await store.get(sid3)

    check("old hash session 1 invalidated", data1 is None)
    check("old hash session 2 invalidated", data2 is None)
    check("new hash session kept", data3 is not None)


async def test_db_store_user_sessions(db):
    """Test getting all sessions for a user."""
    print("\n=== User Sessions List ===")

    store = DatabaseSessionStore(max_age=3600)
    await HyperSession.objects.delete()

    await store.create({"user_id": 7, "device": "laptop"})
    await store.create({"user_id": 7, "device": "phone"})
    await store.create({"user_id": 8, "device": "tablet"})

    user_sessions = await store.get_user_sessions(7)
    check("user 7 has 2 sessions", len(user_sessions) == 2, f"got {len(user_sessions)}")

    user8_sessions = await store.get_user_sessions(8)
    check("user 8 has 1 session", len(user8_sessions) == 1)


async def test_db_store_touch(db):
    """Test touch (extend expiry)."""
    print("\n=== Session Touch ===")

    store = DatabaseSessionStore(max_age=3600)
    await HyperSession.objects.delete()

    sid = await store.create({"user_id": 1})

    # Age the session past its expiry by writing the column, rather than
    # sleeping out a short max_age — a loaded runner oversleeps, and with a
    # short max_age that alone would expire the session AFTER the touch and
    # fail a test the platform passed.
    await HyperSession.objects.filter(session_id=sid).update(
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    check("expired session is not returned", await store.get(sid) is None)

    before = datetime.now(UTC)
    await store.touch(sid)
    row = await HyperSession.objects.filter(session_id=sid).first()
    check(
        "touch re-stamps expiry to now + max_age",
        row.expires_at >= utc_naive(before) + timedelta(seconds=3600),
        f"got {row.expires_at}, touched at {before}",
    )

    data = await store.get(sid)
    check("touched session still valid", data is not None)


async def test_session_auth_with_db_store(db):
    """Test SessionAuth middleware with DatabaseSessionStore."""
    print("\n=== SessionAuth + DatabaseSessionStore ===")

    store = DatabaseSessionStore(max_age=3600)
    await HyperSession.objects.delete()

    sa = SessionAuth(secret="test-secret-key", store=store)

    # Login (async version for DB store)
    resp = Response.json({"ok": True})
    sid = await sa.login_async(resp, {"user_id": 1, "username": "alice"})
    check("login_async returns session ID", isinstance(sid, str))

    # Check cookie was set
    set_cookie = resp.headers.get("set-cookie", "")
    check(
        "cookie set on response", "sessionid=" in set_cookie or "session=" in set_cookie
    )

    # Simulate request with cookie
    cookie_val = set_cookie.split(";")[0].split("=", 1)[1]
    request = Request(
        method="GET",
        path="/protected",
        headers={"cookie": f"{sa.cookie_name}={cookie_val}"},
        query_string="",
        body=b"",
    )

    # Run middleware
    async def handler(req):
        return Response.json({"user": req.user})

    response = await sa(request, handler)
    check("middleware sets request.user", request.user is not None)
    if request.user:
        check("user has username", request.user.get("username") == "alice")

    # Logout (async)
    logout_resp = Response.json({"ok": True})
    await sa.logout_async(logout_resp, request.session_id)

    # Verify session deleted
    data = await store.get(sid)
    check("session deleted after logout", data is None)


async def test_session_auth_with_memory_store():
    """Test SessionAuth middleware with InMemorySessionStore."""
    print("\n=== SessionAuth + InMemorySessionStore ===")

    store = InMemorySessionStore(max_age=3600)
    sa = SessionAuth(secret="test-mem-secret", store=store)

    # Login (sync version)
    resp = Response.json({"ok": True})
    sid = sa.login(resp, {"user_id": 2, "username": "bob"})
    check("login returns session ID", isinstance(sid, str))

    # Simulate request
    set_cookie = resp.headers.get("set-cookie", "")
    cookie_val = set_cookie.split(";")[0].split("=", 1)[1]
    request = Request(
        method="GET",
        path="/test",
        headers={"cookie": f"{sa.cookie_name}={cookie_val}"},
        query_string="",
        body=b"",
    )

    async def handler(req):
        return Response.json({"user": req.user})

    response = await sa(request, handler)
    check("memory store middleware works", request.user is not None)
    if request.user:
        check("user is bob", request.user.get("username") == "bob")


async def test_unlogged_table(db):
    """Verify the table is created as UNLOGGED."""
    print("\n=== UNLOGGED Table Verification ===")

    # Check if table exists and is unlogged
    row = await db.query_one(
        "SELECT relpersistence FROM pg_class WHERE relname = 'hyper_sessions'"
    )
    if row:
        persistence = row.get("relpersistence", "")
        # 'u' = unlogged, 'p' = permanent (regular), 't' = temporary
        check(
            "table is UNLOGGED",
            persistence == "u",
            f"relpersistence={persistence} (expected 'u' for unlogged)",
        )
    else:
        check("hyper_sessions table exists", False, "table not found")


async def main():
    global passed, failed

    db = Database(DB_URL)
    await db.connect()
    set_db(db)

    try:
        # Create table from HyperSession model (UNLOGGED, with indexes)
        await create_table_for_model(HyperSession, db=db, drop=True)

        await test_in_memory_store()
        await test_db_store_crud(db)
        await test_db_store_expiry(db)
        await test_db_store_cleanup(db)
        await test_db_store_user_invalidation(db)
        await test_db_store_hash_invalidation(db)
        await test_db_store_user_sessions(db)
        await test_db_store_touch(db)
        await test_session_auth_with_db_store(db)
        await test_session_auth_with_memory_store()
        await test_unlogged_table(db)
    finally:
        await db.disconnect()

    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed == 0:
        print("All database session tests passed!")
    else:
        print(f"{failed} tests need attention")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
