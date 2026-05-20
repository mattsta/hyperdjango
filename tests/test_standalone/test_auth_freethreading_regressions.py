"""Regression tests for the ws18 auth free-threading + fail-open fixes.

These lock in three fixes, each empirically reproduced on the free-threaded
3.14t build before the fix:

1. [F1 — security] ``build_session_data`` used to swallow an RBAC field-access
   load error and cache ``field_access={}``. The fix fails CLOSED: the RBAC
   error propagates and aborts login instead of caching a known-incomplete map.
   (Historically an empty map ALSO fail-opened, because
   ``Require.field_access`` defaulted an ABSENT field to "writable"; ws27 item
   5a changed that default to the most restrictive level, but not caching a
   partial map on a transient error remains correct.)

2. [race] ``InMemorySessionStore`` mutated ``_sessions`` / ``_expiry_index``
   (a SortedList) / ``_user_index`` with zero synchronization. Under
   free-threading this produced IndexError / KeyError / set-changed-size
   crashes and index divergence (orphans → missed session revocation). The fix
   guards every read/mutate with one lock.

3. [race — TOCTOU] OAuth2 replay-nonce check-and-add had no lock, so two
   concurrent callbacks with the same state both passed. The fix makes the
   check-and-add atomic; concurrent duplicates → exactly one winner.

Run: uv run pytest tests/test_standalone/test_auth_freethreading_regressions.py -q
"""

import random
import threading
from collections import Counter
from unittest.mock import patch

import pytest

from hyperdjango.auth import sessions as S
from hyperdjango.auth.oauth2 import OAuth2
from hyperdjango.auth.sessions import InMemorySessionStore, build_session_data
from hyperdjango.exceptions import HTTPException

# ── 1. F1: RBAC-load error must fail CLOSED (no writable-everything) ──────────


class _BoomFieldAccessChecker:
    """PermissionChecker stub whose field-access query raises (transient DB)."""

    def __init__(self, db):
        pass

    async def get_user_group_names(self, uid):
        return ["staff"]

    async def get_all_field_access(self, uid):
        raise RuntimeError("transient DB failure")


async def test_field_access_load_error_fails_closed():
    """A transient RBAC error during login must NOT produce field_access={}.

    Pre-fix: the error was swallowed, field_access={} was cached, and guard
    then granted "writable" on every field. Post-fix: build_session_data
    propagates the error and login is aborted — no over-permissive session is
    ever minted.
    """
    with (
        patch.object(S, "PermissionChecker", _BoomFieldAccessChecker),
        pytest.raises(RuntimeError, match="transient DB failure"),
    ):
        await build_session_data(7, db=object(), username="mallory")


async def test_field_access_success_populates_map():
    """The happy path still caches a real field-access map."""

    class _OKChecker:
        def __init__(self, db):
            pass

        async def get_user_group_names(self, uid):
            return ["staff"]

        async def get_all_field_access(self, uid):
            return {"employee": {"salary": "readonly"}}

        async def _get_all_permissions(self, user):
            return set()

    with patch.object(S, "PermissionChecker", _OKChecker):
        sess = await build_session_data(7, db=object(), username="alice")
    assert sess["field_access"] == {"employee": {"salary": "readonly"}}
    assert sess["is_staff"] is True


async def test_superuser_skips_field_access_query():
    """Superusers bypass field restrictions, so the field query is not run
    and cannot fail the login."""

    class _BoomOnFieldChecker:
        def __init__(self, db):
            pass

        async def get_user_group_names(self, uid):
            return ["superuser"]

        async def get_all_field_access(self, uid):
            raise AssertionError("must not be called for superuser")

    with patch.object(S, "PermissionChecker", _BoomOnFieldChecker):
        sess = await build_session_data(1, db=object(), username="root")
    assert sess["is_superuser"] is True
    assert sess["field_access"] == {}


# ── 2. InMemorySessionStore concurrency ──────────────────────────────────────


def test_session_store_concurrent_stress_no_crash_no_orphans():
    """12 threads × 3000 mixed ops must not crash and must leave the three
    indexes consistent (no orphaned expiry / user-index entries)."""
    store = InMemorySessionStore(max_age=0.005)  # tiny -> constant expiry churn
    n_threads, iters = 12, 3000
    errors: list[str] = []
    elock = threading.Lock()
    sids: list[str] = []
    slock = threading.Lock()

    def worker(tid: int) -> None:
        rng = random.Random(tid)
        for i in range(iters):
            try:
                op = rng.random()
                if op < 0.35:
                    sid = store.create({"user_id": rng.randrange(20), "n": i})
                    with slock:
                        sids.append(sid)
                        if len(sids) > 400:
                            sids.pop(0)
                elif op < 0.6:
                    with slock:
                        sid = rng.choice(sids) if sids else None
                    if sid:
                        store.get(sid)
                elif op < 0.75:
                    with slock:
                        sid = rng.choice(sids) if sids else None
                    if sid:
                        store.update(sid, {"user_id": rng.randrange(20), "n": -i})
                elif op < 0.85:
                    with slock:
                        sid = rng.choice(sids) if sids else None
                    if sid:
                        store.delete(sid)
                elif op < 0.95:
                    store.cleanup()
                else:
                    store.invalidate_for_user(rng.randrange(20))
            except Exception as e:  # noqa: BLE001 - we assert none occur
                with elock:
                    errors.append(f"{type(e).__name__}: {e}")

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    orphan_expiry = sum(
        1 for _, sid in list(store._expiry_index) if sid not in store._sessions
    )
    orphan_user = sum(
        1
        for _uid, group in store._user_index.items()
        for sid in group
        if sid not in store._sessions
    )
    assert not errors, f"{len(errors)} errors: {Counter(errors).most_common(5)}"
    assert orphan_expiry == 0, f"{orphan_expiry} orphaned expiry-index entries"
    assert orphan_user == 0, f"{orphan_user} orphaned user-index entries"


def test_invalidate_for_user_removes_all_sessions():
    """invalidate_for_user must drop every one of the user's sessions from all
    three indexes — the session-revocation-on-password-change path."""
    store = InMemorySessionStore(max_age=3600)
    sids = [store.create({"user_id": 42, "n": i}) for i in range(5)]
    store.create({"user_id": 99, "n": 0})  # bystander must survive

    store.invalidate_for_user(42)

    for sid in sids:
        assert store.get(sid) is None
    assert 42 not in store._user_index or not store._user_index[42]
    assert all(sid not in store._sessions for sid in sids)
    # No orphaned expiry entries left behind for the revoked user
    assert all(s in store._sessions for _, s in list(store._expiry_index))
    # Bystander untouched
    assert store.count() == 1


def test_invalidate_for_user_under_concurrent_creates():
    """A password-change revocation racing with fresh logins for the same user
    must not leave live sessions behind for that user (index divergence bug)."""
    store = InMemorySessionStore(max_age=3600)
    uid = 7
    stop = threading.Event()

    def creator() -> None:
        while not stop.is_set():
            store.create({"user_id": uid})

    makers = [threading.Thread(target=creator) for _ in range(6)]
    for t in makers:
        t.start()
    for _ in range(200):
        store.invalidate_for_user(uid)
    stop.set()
    for t in makers:
        t.join()

    # Final revocation after all creators stopped must clear the user entirely.
    store.invalidate_for_user(uid)
    live = [s for s in store._user_index.get(uid, set()) if s in store._sessions]
    assert live == [], f"{len(live)} live sessions survived revocation"


# ── 3. OAuth2 replay-nonce concurrency (TOCTOU) ──────────────────────────────


def test_concurrent_same_state_exactly_one_winner():
    """Many threads consuming the SAME state concurrently: exactly one wins,
    all others are rejected as replays."""
    for _ in range(100):
        oauth = OAuth2(secret="x")
        state = "identical-state-token"
        barrier = threading.Barrier(16)
        results: list[str] = []
        rlock = threading.Lock()

        def attempt() -> None:
            barrier.wait()  # maximise the concurrent window
            try:
                oauth._consume_nonce(state)
                with rlock:
                    results.append("ok")
            except HTTPException:
                with rlock:
                    results.append("replay")

        threads = [threading.Thread(target=attempt) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert results.count("ok") == 1, f"expected 1 winner, got {results.count('ok')}"


def test_nonce_eviction_is_bounded_fifo_not_bulk_clear():
    """The size cap must evict the OLDEST nonces by insertion order and keep
    recent ones — never a blanket clear() that reopens the replay window."""
    oauth = OAuth2(secret="x")
    oauth._max_nonces = 10
    for i in range(25):
        oauth._consume_nonce(f"s{i}")
    # Bounded to the cap
    assert len(oauth._used_nonces) == 10
    # Oldest evicted, most-recent retained
    assert "s0" not in oauth._used_nonces
    assert "s24" in oauth._used_nonces
    # A still-retained recent nonce is still protected against replay
    with pytest.raises(HTTPException):
        oauth._consume_nonce("s24")
