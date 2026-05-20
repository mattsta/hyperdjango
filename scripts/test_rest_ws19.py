#!/usr/bin/env python
"""ws19-rest-py: regression tests for free-threading races + a functional bug in
hyperdjango.rest server-side cursor pagination.

Covers, with live PostgreSQL where needed:
  #5  end-to-end DECLARE CURSOR pagination over a real queryset (the path that
      _build_select() mis-use had left completely broken).
  #2  concurrent replay of the same cursor token -> 409 Conflict (or serialized),
      never interleaved wire-protocol corruption; cleanup never closes an in-use
      cursor.
  #1  concurrent open / close / over-limit / expiry never deadlocks (no await is
      held under the registry threading.Lock).
  #4  _remove_cursor decrements the per-user count only when it actually removed
      an entry (no double-decrement eroding the pool-exhaustion guard).
  #3  _get_cursor_secret resolves exactly ONE secret under concurrency and does
      not mutate os.environ; SimpleRateThrottle backend inits once.

Run:  DATABASE_URL=postgres://localhost:5432/hyperdjango_test uv run python scripts/test_rest_ws19.py
"""

# hyper-test: db_isolated

from __future__ import annotations

import asyncio
import base64
import os
import threading
import time

import hyperdjango.rest as rest
from hyperdjango.auth.user import SessionUser
from hyperdjango.database import Database, set_db
from hyperdjango.models import Field, Model
from hyperdjango.request import Request
from hyperdjango.rest import (
    Conflict,
    ServerCursorPagination,
    SimpleRateThrottle,
    Throttled,
    _active_server_cursors,
    _get_cursor_secret,
    _user_cursor_counts,
    cleanup_expired_server_cursors,
)

DB_URL = os.environ.get("DATABASE_URL", "postgres://localhost:5432/hyperdjango_test")

_passed = 0
_failed = 0


def check(name: str, condition: bool) -> None:
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"  ✓ {name}")
    else:
        _failed += 1
        print(f"  ✗ {name}")


def make_request(query_string: str = "", user=None) -> Request:
    req = Request(
        method="GET",
        path="/",
        query_string=query_string,
        body=b"",
        headers={},
        path_params={},
    )
    req.user = user
    return req


class SCItem(Model):
    class Meta:
        table = "test_sc_ws19"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field()
    value: int = Field()


async def _seed(db: Database, n: int = 55) -> None:
    await db.execute("DROP TABLE IF EXISTS test_sc_ws19 CASCADE")
    await db.execute(
        """
        CREATE TABLE test_sc_ws19 (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            value INTEGER NOT NULL
        )
        """
    )
    for i in range(1, n + 1):
        await db.execute(
            "INSERT INTO test_sc_ws19 (name, value) VALUES ($1, $2)",
            f"item_{i}",
            i * 10,
        )


# ── #5: end-to-end DECLARE CURSOR pagination over a queryset ────────────────────
async def test_e2e_pagination(db: Database) -> None:
    print("\n── #5 end-to-end server-cursor pagination over a queryset ──")
    _active_server_cursors.clear()
    _user_cursor_counts.clear()

    user = SessionUser({"id": 7})

    class Pag(ServerCursorPagination):
        page_size = 10

    # Page 1 — no token: opens a REAL DECLARE CURSOR and FETCHes the first page.
    pag = Pag()
    rows = await pag.paginate_queryset(
        SCItem.objects.order_by("id"), make_request(user=user)
    )
    # Exercise the response envelope too (must not raise).
    pag.get_paginated_response(rows)
    check("page 1 returns 10 rows (DECLARE CURSOR path works at all)", len(rows) == 10)
    check("page 1 first id == 1", rows[0]["id"] == 1)
    check("page 1 last id == 10", rows[9]["id"] == 10)
    check("page 1 not exhausted", pag._is_exhausted is False)
    token = pag._cursor_id
    check("page 1 issued a cursor token", isinstance(token, str) and len(token) > 0)

    # Walk the rest of the pages by replaying the token.
    seen_ids: list[int] = [r["id"] for r in rows]
    guard = 0
    while token is not None and guard < 100:
        guard += 1
        p = Pag()
        page = await p.paginate_queryset(
            SCItem.objects.order_by("id"),
            make_request(f"server_cursor={token}", user=user),
        )
        seen_ids.extend(r["id"] for r in page)
        token = p._cursor_id
        if p._is_exhausted:
            break

    check("paginated all 55 rows end-to-end", len(seen_ids) == 55)
    check("row ids are contiguous 1..55 in order", seen_ids == list(range(1, 56)))
    check("no duplicate rows across pages", len(set(seen_ids)) == 55)
    check("registry empty after exhaustion", len(_active_server_cursors) == 0)
    check("user count back to 0 after exhaustion", _user_cursor_counts.get("7", 0) == 0)


# ── #2: concurrent same-token replay -> 409, never corruption ───────────────────
async def test_in_use_guard(db: Database) -> None:
    print("\n── #2 in-use guard: concurrent same-token replay ──")
    _active_server_cursors.clear()
    _user_cursor_counts.clear()

    user = SessionUser({"id": 11})

    class Pag(ServerCursorPagination):
        page_size = 5

    # Open a cursor.
    pag = Pag()
    await pag.paginate_queryset(SCItem.objects.order_by("id"), make_request(user=user))
    token = pag._cursor_id
    # Recover the raw cursor_id used as the registry key.
    cursor_id = base64.urlsafe_b64decode(token).decode()

    # Deterministic guard: simulate an in-flight peer by marking in_use, then a
    # replay must be rejected with 409 rather than touching the pinned conn.
    _active_server_cursors[cursor_id]["in_use"] = True
    got_conflict = False
    try:
        await Pag().paginate_queryset(
            SCItem.objects.order_by("id"),
            make_request(f"server_cursor={token}", user=user),
        )
    except Conflict as e:
        got_conflict = True
        check("Conflict carries 409 status", e.status_code == 409)
    check("in-use cursor replay -> 409 Conflict", got_conflict)

    # cleanup must NOT close an in-use cursor even when it looks expired.
    st = _active_server_cursors[cursor_id]
    st["created_at"] = time.time() - 10**9
    st["last_accessed"] = time.time() - 10**9
    cleaned = await cleanup_expired_server_cursors()
    check("cleanup skips in-use cursor (0 closed)", cleaned == 0)
    check(
        "in-use cursor still registered after cleanup",
        cursor_id in _active_server_cursors,
    )

    # Release the claim; a subsequent replay now succeeds (serialized access).
    st["in_use"] = False
    st["created_at"] = time.time()
    st["last_accessed"] = time.time()
    p2 = Pag()
    page = await p2.paginate_queryset(
        SCItem.objects.order_by("id"), make_request(f"server_cursor={token}", user=user)
    )
    check("after release, replay succeeds", len(page) == 5)
    check(
        "replay returned the SECOND page (ids 6..10, no interleave)",
        [r["id"] for r in page] == [6, 7, 8, 9, 10],
    )

    # Drain + close.
    tok = p2._cursor_id
    while tok is not None:
        pp = Pag()
        await pp.paginate_queryset(
            SCItem.objects.order_by("id"),
            make_request(f"server_cursor={tok}", user=user),
        )
        tok = pp._cursor_id


# ── #2 (cross-thread): real OS-thread concurrency, mutual exclusion proven ──────
class _OverlapCursor:
    """Fake DatabaseServerCursor that detects concurrent fetch_page() calls.

    A real pinned PG connection cannot be driven from two OS threads at once
    (that is exactly the wire-protocol corruption we're guarding against), so we
    use a fake here: it records the maximum number of fetch_page() bodies that
    were ever in flight simultaneously. With the in_use guard, that maximum must
    stay 1.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.current = 0
        self.max_concurrent = 0
        self._closed = False
        self._exhausted = False

    async def fetch_page(self):
        with self._lock:
            self.current += 1
            self.max_concurrent = max(self.max_concurrent, self.current)
        # Hold the "connection" long enough for other threads to contend.
        await asyncio.sleep(0.05)
        with self._lock:
            self.current -= 1
        return [{"id": 1}]

    @property
    def is_exhausted(self) -> bool:
        return self._exhausted

    async def close(self) -> None:
        self._closed = True


def test_in_use_threaded() -> None:
    print("\n── #2 in-use guard under real OS-thread concurrency ──")
    import hashlib as _hashlib
    import hmac as _hmac

    _active_server_cursors.clear()
    _user_cursor_counts.clear()

    user = SessionUser({"id": 13})
    user_id = "13"

    # Register one cursor with a fake connection and a VALID HMAC token.
    secret = _get_cursor_secret()
    raw_id = f"{user_id}:abc123:{time.time()}"
    sig = _hmac.new(secret.encode(), raw_id.encode(), _hashlib.sha256).hexdigest()[:32]
    cursor_id = f"{raw_id}:{sig}"
    token = base64.urlsafe_b64encode(cursor_id.encode()).decode()

    overlap = _OverlapCursor()
    now = time.time()
    _active_server_cursors[cursor_id] = {
        "user_id": user_id,
        "created_at": now,
        "last_accessed": now,
        "total_fetched": 0,
        "db_cursor": overlap,
        "in_use": False,
    }
    _user_cursor_counts[user_id] = 1

    results: list[tuple] = []
    rlock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()  # maximize the race window

        async def _fetch():
            pag = ServerCursorPagination()
            return await pag._fetch_existing_cursor(
                token, make_request(f"server_cursor={token}", user=user)
            )

        try:
            page = asyncio.run(_fetch())
            with rlock:
                results.append(("ok", page))
        except Conflict:
            with rlock:
                results.append(("conflict", None))
        except Exception as e:  # noqa: BLE001 - surface any unexpected failure
            with rlock:
                results.append(("error", repr(e)))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    check("no thread deadlocked (all joined)", all(not t.is_alive() for t in threads))
    kinds = [r[0] for r in results]
    check("all 8 workers returned a result", len(results) == 8)
    check("no unexpected errors", "error" not in kinds)
    check("at least one worker fetched, others 409'd", kinds.count("ok") >= 1)
    check(
        "fetch_page never ran concurrently (max_concurrent == 1, no wire interleave)",
        overlap.max_concurrent == 1,
    )
    check("conflicts observed under contention", kinds.count("conflict") >= 1)


# ── #1: over-limit + expiry never deadlock, close happens outside lock ──────────
async def test_no_deadlock(db: Database) -> None:
    print("\n── #1 over-limit + expiry close outside the lock (no deadlock) ──")
    _active_server_cursors.clear()
    _user_cursor_counts.clear()

    user = SessionUser({"id": 21})

    class Pag(ServerCursorPagination):
        page_size = 5
        max_per_user = 3

    # Open max_per_user cursors.
    tokens = []
    for _ in range(3):
        pag = Pag()
        await asyncio.wait_for(
            pag.paginate_queryset(
                SCItem.objects.order_by("id"), make_request(user=user)
            ),
            timeout=10,
        )
        tokens.append(pag._cursor_id)
    check("opened up to the per-user limit", _user_cursor_counts.get("21", 0) == 3)

    # 4th open must be Throttled WITHOUT hanging (old code awaited close() under lock).
    throttled = False
    try:
        await asyncio.wait_for(
            Pag().paginate_queryset(
                SCItem.objects.order_by("id"), make_request(user=user)
            ),
            timeout=10,
        )
    except Throttled:
        throttled = True
    check("over-limit open -> Throttled, no deadlock", throttled)
    check("limit unchanged after rejected open", _user_cursor_counts.get("21", 0) == 3)

    # Expiry path: force a cursor stale, replay -> NotFound after close-outside-lock.
    cid = base64.urlsafe_b64decode(tokens[0]).decode()
    _active_server_cursors[cid]["last_accessed"] = time.time() - 10**9
    not_found = False
    try:
        await asyncio.wait_for(
            Pag().paginate_queryset(
                SCItem.objects.order_by("id"),
                make_request(f"server_cursor={tokens[0]}", user=user),
            ),
            timeout=10,
        )
    except rest.NotFound:
        not_found = True
    check("idle-expired replay -> NotFound, no deadlock", not_found)
    check(
        "expired cursor decremented the user count",
        _user_cursor_counts.get("21", 0) == 2,
    )

    # Drain remaining to release pinned connections.
    for tok in tokens[1:]:
        t = tok
        while t is not None:
            pp = Pag()
            await pp.paginate_queryset(
                SCItem.objects.order_by("id"),
                make_request(f"server_cursor={t}", user=user),
            )
            t = pp._cursor_id


# ── #4: _remove_cursor double-decrement guard ───────────────────────────────────
def test_remove_no_double_decrement() -> None:
    print("\n── #4 _remove_cursor decrements only on real removal ──")
    _active_server_cursors.clear()
    _user_cursor_counts.clear()

    pag = ServerCursorPagination()
    # User u has two live cursors.
    _active_server_cursors["cx"] = {"user_id": "u"}
    _active_server_cursors["cy"] = {"user_id": "u"}
    _user_cursor_counts["u"] = 2

    with rest._cursor_registry_lock:
        pag._remove_cursor("cx", "u")  # real removal -> 1
    check("first removal decrements to 1", _user_cursor_counts["u"] == 1)

    with rest._cursor_registry_lock:
        pag._remove_cursor("cx", "u")  # already gone -> must NOT decrement
    check(
        "second (no-op) removal does NOT double-decrement (still 1, cy alive)",
        _user_cursor_counts["u"] == 1,
    )
    check("cy still counts against the pool guard", "cy" in _active_server_cursors)


# ── #3: cursor-secret DCL under threads + no os.environ mutation ────────────────
def test_secret_dcl() -> None:
    print("\n── #3 _get_cursor_secret: single secret, no os.environ write ──")
    # Force the unset-env ephemeral path.
    prev_cursor = os.environ.pop("HYPER_CURSOR_SECRET", None)
    prev_key = os.environ.pop("HYPER_SECRET_KEY", None)
    rest._cached_cursor_secret = None
    try:
        results: list[str] = []
        lock = threading.Lock()
        barrier = threading.Barrier(16)

        def worker():
            barrier.wait()
            s = _get_cursor_secret()
            with lock:
                results.append(s)

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        check(
            "all 16 threads observed the SAME secret (no split-brain)",
            len(set(results)) == 1 and len(results) == 16,
        )
        check(
            "ephemeral secret NOT written back to os.environ",
            "HYPER_CURSOR_SECRET" not in os.environ,
        )
    finally:
        rest._cached_cursor_secret = None
        if prev_cursor is not None:
            os.environ["HYPER_CURSOR_SECRET"] = prev_cursor
        if prev_key is not None:
            os.environ["HYPER_SECRET_KEY"] = prev_key


def test_throttle_backend_dcl() -> None:
    print("\n── #3 SimpleRateThrottle backend inits once under threads ──")
    SimpleRateThrottle._backend = None

    class T(SimpleRateThrottle):
        rate = "100/hour"

    backends: list[int] = []
    lock = threading.Lock()
    barrier = threading.Barrier(16)

    def worker():
        barrier.wait()
        t = T()
        with lock:
            backends.append(id(t._backend))

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    check("throttle backend is a single shared instance", len(set(backends)) == 1)
    check("throttle backend is not None", SimpleRateThrottle._backend is not None)


def main() -> int:
    # Pure-logic tests (no DB).
    test_remove_no_double_decrement()
    test_secret_dcl()
    test_throttle_backend_dcl()

    # DB-backed tests. The threaded test spawns worker threads that each call
    # asyncio.run(), so it must NOT run inside a running loop — hence separate
    # top-level asyncio.run() phases sharing one process-global connection.
    db = Database(DB_URL, max_size=10)
    asyncio.run(db.connect())
    set_db(db)
    try:
        asyncio.run(_seed(db, 55))
        asyncio.run(test_e2e_pagination(db))
        asyncio.run(test_in_use_guard(db))
        test_in_use_threaded()  # sync: owns its own per-thread loops, fake cursor
        asyncio.run(test_no_deadlock(db))
    finally:
        asyncio.run(db.execute("DROP TABLE IF EXISTS test_sc_ws19 CASCADE"))
        asyncio.run(db.disconnect())

    print("\n" + "=" * 60)
    print(
        f"ws19 rest tests: {_passed} passed, {_failed} failed ({_passed + _failed} total)"
    )
    print("=" * 60)
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
