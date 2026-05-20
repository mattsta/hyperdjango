"""Regression tests for round-11 hot-path resource leaks.

Pure-Python (no native build, no DB, no full suite). Run with:

    uv run python scripts/test_leaks_r11.py

Covers three confirmed leaks and their fixes:

1. request.py  — disk-spilled ``UploadedFile`` temp files were never deleted.
   Asserts cleanup()/close()/__del__ unlink the temp, idempotently, AND that a
   size/count limit raised mid-parse unlinks every already-spilled temp.
2. standalone_middleware.py — ``RateLimitMiddleware`` held a threading.Lock
   across the 429-path telemetry ``await``. Asserts NO shard lock is held while
   ``log_from_request`` is awaited.
3. rest.py — ``ServerCursorPagination._create_new_cursor`` leaked the pinned
   pool connection when the first ``fetch_page()`` failed. Asserts the cursor is
   closed (connection released) with no registry entry left behind, and that the
   over-limit path still closes exactly once (no double-close).
"""

# hyper-test: db_shared

import asyncio
import gc
import tempfile
import traceback
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

from hyperdjango.testkit import check, finish, run_main


def _uploads_in(dirpath):
    return [p for p in Path(dirpath).iterdir() if p.name.startswith("hyper_upload_")]


# --------------------------------------------------------------------------
# Finding 1: UploadedFile temp-file lifecycle (request.py)
# --------------------------------------------------------------------------
def test_uploadedfile_temp_cleanup():
    import hyperdjango.request as reqmod
    from hyperdjango.exceptions import HTTPException
    from hyperdjango.request import Request

    orig_get_setting = reqmod.get_setting

    def install_settings(**overrides):
        base = {
            "FILE_UPLOAD_MAX_MEMORY_SIZE": 10,  # spill anything > 10 bytes
            "FILE_UPLOAD_MAX_SIZE": 0,  # disabled unless overridden
            "FILE_UPLOAD_TEMP_DIR": tmpdir,
            "DATA_UPLOAD_MAX_NUMBER_FIELDS": 0,  # disabled unless overridden
            "DATA_UPLOAD_MAX_NUMBER_FILES": 0,  # disabled unless overridden
            "STREAM_BODY_CHUNK_SIZE": 65536,
        }
        base.update(overrides)

        def fake(name, default=None):
            return base.get(name, default)

        reqmod.get_setting = fake

    def parse(parts):
        req = Request(method="POST")
        req._multipart_parts = parts
        req._parse_multipart()
        return req

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            # (a) explicit cleanup() unlinks the temp, idempotently -----------
            install_settings()
            req = parse([("f", "big.bin", "application/octet-stream", b"x" * 100)])
            uf = req._files["f"]
            assert uf.path is not None and Path(uf.path).exists(), (
                "spilled upload must have an on-disk temp file"
            )
            path = uf.path
            uf.cleanup()
            assert not Path(path).exists(), "cleanup() must unlink the temp file"
            assert uf.path is None, "cleanup() must clear _path (idempotent guard)"
            uf.cleanup()  # second call must be a no-op, never raise
            uf.close()  # alias must also be a no-op

            # (b) __del__ backstop reclaims a temp the caller forgot ----------
            req2 = parse([("f", "gc.bin", "application/octet-stream", b"y" * 100)])
            uf2 = req2._files["f"]
            path2 = uf2.path
            assert Path(path2).exists()
            del uf2
            del req2
            gc.collect()
            assert not Path(path2).exists(), "__del__ must reclaim orphaned temp"

            # (c) DATA_UPLOAD_MAX_NUMBER_FILES raise unlinks spilled temps ----
            install_settings(DATA_UPLOAD_MAX_NUMBER_FILES=1)
            assert _uploads_in(tmpdir) == [], "temp dir must be clean before parse"
            raised = False
            try:
                parse(
                    [
                        ("a", "a.bin", "application/octet-stream", b"a" * 100),
                        ("b", "b.bin", "application/octet-stream", b"b" * 100),
                    ]
                )
            except HTTPException as e:
                raised = True
                assert e.status_code == 400
            assert raised, "too-many-files must raise"
            assert _uploads_in(tmpdir) == [], (
                "file-count-limit raise must unlink already-spilled temps"
            )

            # (d) per-file 413 raised MID-loop unlinks earlier spilled temps --
            install_settings(FILE_UPLOAD_MAX_SIZE=50)
            assert _uploads_in(tmpdir) == []
            raised = False
            try:
                parse(
                    [
                        # first part: 30 bytes > mem(10), < max(50) -> spills OK
                        ("a", "a.bin", "application/octet-stream", b"a" * 30),
                        # second part: 100 bytes > max(50) -> 413 mid-loop
                        ("b", "b.bin", "application/octet-stream", b"b" * 100),
                    ]
                )
            except HTTPException as e:
                raised = True
                assert e.status_code == 413
            assert raised, "oversized file must raise 413"
            assert _uploads_in(tmpdir) == [], (
                "mid-loop 413 must unlink the temp spilled for the earlier part"
            )
        finally:
            reqmod.get_setting = orig_get_setting


# --------------------------------------------------------------------------
# Finding 2: RateLimitMiddleware must not hold the lock across the await
# --------------------------------------------------------------------------
async def test_ratelimit_no_lock_across_await():
    import hyperdjango.ratelimit as mw
    from hyperdjango.ratelimit import RateLimitMiddleware

    rl = RateLimitMiddleware(max_requests=1, window=60, key_func=lambda r: "fixed-key")

    lock_states = []

    class StubSecLog:
        async def log_from_request(self, event, request, detail=""):
            # Prove the backend's shard lock is released BEFORE the telemetry await runs.
            lock_states.append(any(lock.locked() for lock in rl.backend._locks))

    orig = mw._get_security_log
    mw._get_security_log = lambda: StubSecLog()
    try:

        async def call_next(request):
            return SimpleNamespace(headers={})

        req = SimpleNamespace()
        # 1st request consumes the single-slot quota (allowed path).
        await rl(req, call_next)
        # 2nd request is over-limit -> hits the telemetry await + 429 build.
        resp = await rl(req, call_next)

        assert lock_states == [False], (
            f"telemetry await must run with NO shard lock held, got {lock_states}"
        )
        assert resp.status == 429, f"over-limit must return 429, got {resp.status}"
        # After the whole call, every lock must be released too.
        assert not any(lock.locked() for lock in rl.backend._locks)
    finally:
        mw._get_security_log = orig


# --------------------------------------------------------------------------
# Finding 3: server-cursor first-fetch failure releases the pinned connection
# --------------------------------------------------------------------------
async def test_server_cursor_first_fetch_release():
    import hyperdjango.rest as rest
    from hyperdjango.rest import (
        ServerCursorPagination,
        Throttled,
        _active_server_cursors,
    )

    orig_secret = rest._get_cursor_secret
    rest._get_cursor_secret = lambda: "test-secret"
    try:
        # (a) first fetch_page() fails -> cursor closed, nothing registered ---
        class FailingCursor:
            def __init__(self):
                self.closed = 0
                self.is_exhausted = False

            async def fetch_page(self):
                raise RuntimeError("statement timeout surfaced at first FETCH")

            async def close(self):
                self.closed += 1

        fc = FailingCursor()

        class FakeQS:
            def __init__(self, cursor):
                self._cursor = cursor

            def _get_db(self):
                cursor = self._cursor

                class DB:
                    async def server_cursor(self, sql, params, page_size=None):
                        return cursor

                return DB()

            def _build_select(self):
                return ("SELECT 1", [])

        p = ServerCursorPagination()
        req = SimpleNamespace(user=None, client_ip="1.2.3.4", GET={})
        before = len(_active_server_cursors)

        raised = False
        try:
            await p._create_new_cursor(FakeQS(fc), req)
        except RuntimeError:
            raised = True
        assert raised, "first-fetch failure must propagate"
        assert fc.closed == 1, (
            "failed first fetch must close the cursor exactly once "
            f"(pinned connection released), got closed={fc.closed}"
        )
        assert len(_active_server_cursors) == before, (
            "no registry entry may leak on first-fetch failure"
        )

        # (b) over-limit path still closes exactly once (no double-close) -----
        class OkCursor:
            def __init__(self):
                self.closed = 0
                self.is_exhausted = False

            async def fetch_page(self):
                return [1, 2, 3]

            async def close(self):
                self.closed += 1

        oc = OkCursor()
        p2 = ServerCursorPagination()
        p2.max_per_user = 0  # force the over-limit branch
        req2 = SimpleNamespace(user=None, client_ip="9.9.9.9", GET={})

        throttled = False
        try:
            await p2._create_new_cursor(FakeQS(oc), req2)
        except Throttled:
            throttled = True
        assert throttled, "over-limit must raise Throttled"
        assert oc.closed == 1, (
            f"over-limit must close exactly once (no double-close), got {oc.closed}"
        )
        assert len(_active_server_cursors) == before, "no registry entry may leak"
    finally:
        rest._get_cursor_secret = orig_secret


def _run(name: str, fn: Callable[[], None]) -> bool:
    """Run one assert-battery function and record a single pass/fail.

    The asserts inside each function abort it on the first bad value — this
    file's contract — so a failure is reported once, at function granularity,
    and the caller stops.
    """
    try:
        fn()
    except Exception as exc:
        check(name, False, f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
        return False
    return check(name, True)


def main() -> bool:
    stages: tuple[tuple[str, Callable[[], None]], ...] = (
        (
            "finding 1: UploadedFile temp cleanup (explicit, __del__, limit raises)",
            test_uploadedfile_temp_cleanup,
        ),
        (
            "finding 2: RateLimit 429 telemetry await runs outside the shard lock",
            lambda: asyncio.run(test_ratelimit_no_lock_across_await()),
        ),
        (
            "finding 3: server-cursor first-fetch failure releases pinned connection",
            lambda: asyncio.run(test_server_cursor_first_fetch_release()),
        ),
    )
    for name, fn in stages:
        if not _run(name, fn):
            return finish()
    print()
    return finish()


if __name__ == "__main__":
    run_main(main)
