"""
Platform-wide security regression tests.

Covers fixes from Categories 4+5 of the full-platform audit:
1. Template path traversal prevention
2. XSS in linebreaks filter
3. LRU cache thread safety
4. SQL injection prevention (parameterized INTERVAL)
5. Cache ttl=0 correctness
6. Session INTERVAL parameterization
7. Security log INTERVAL parameterization
8. Rate limit INTERVAL parameterization

Usage:
    uv run hyper-test platform_security
"""

# hyper-test: db_isolated

import asyncio
import inspect
import os
import sys
import time
import traceback
from pathlib import Path

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
# Template path traversal
# ---------------------------------------------------------------------------


@test("template: path traversal via ../ blocked")
def test_template_path_traversal():
    from hyperdjango.templating import TemplateEngine

    engine = TemplateEngine("templates")

    try:
        engine._load_template_source("../../../etc/passwd")
        assert False, "Should have raised FileNotFoundError"
    except FileNotFoundError as e:
        assert "escapes" in str(e).lower() or "not found" in str(e).lower()


@test("template: path traversal via encoded ../ blocked")
def test_template_path_traversal_encoded():
    from hyperdjango.templating import TemplateEngine

    engine = TemplateEngine("templates")

    try:
        engine._load_template_source("..%2f..%2f..%2fetc/passwd")
        assert False, "Should have raised FileNotFoundError"
    except FileNotFoundError:
        pass  # Either "escapes" or "not found" is correct


@test("template: normal template path allowed")
def test_template_path_normal():
    import shutil
    import tempfile

    from hyperdjango.templating import TemplateEngine

    tmpdir = tempfile.mkdtemp()
    try:
        sub_dir = Path(tmpdir) / "sub"
        sub_dir.mkdir(parents=True, exist_ok=True)
        (sub_dir / "test.html").write_text("<h1>OK</h1>")

        engine = TemplateEngine(tmpdir)
        source = engine._load_template_source("sub/test.html")
        assert "<h1>OK</h1>" in source
    finally:
        shutil.rmtree(tmpdir)


# ---------------------------------------------------------------------------
# XSS in linebreaks filter
# ---------------------------------------------------------------------------


@test("linebreaks: escapes HTML before adding <br>")
def test_linebreaks_xss():
    from hyperdjango.serving.template_compat import linebreaks_filter

    result = linebreaks_filter("<script>alert(1)</script>\nHello")
    assert "<script>" not in result
    assert "&lt;script&gt;" in result
    assert "<br>" in result


@test("linebreaks: safe text unchanged except newlines")
def test_linebreaks_safe():
    from hyperdjango.serving.template_compat import linebreaks_filter

    result = linebreaks_filter("Hello\nWorld")
    assert result == "Hello<br>World"


# ---------------------------------------------------------------------------
# LRU cache thread safety
# ---------------------------------------------------------------------------


@test("LRU cache: get_mtime is thread-safe")
def test_lru_thread_safe():
    import threading

    from hyperdjango.templating import _LRUCache

    cache = _LRUCache(max_bytes=1024 * 1024)
    errors = []

    def writer():
        for i in range(100):
            cache.put(f"key_{i}", f"value_{i}", source_size=10, mtime=float(i))

    def reader():
        for i in range(100):
            cache.get_mtime(f"key_{i}")
            len(cache)
            _ = cache.total_bytes

    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Should not crash under concurrent access
    assert cache.count >= 0


# ---------------------------------------------------------------------------
# Cache ttl=0 correctness
# ---------------------------------------------------------------------------


@test("LocMemCache: ttl=0 means immediate expiry, not infinite")
def test_cache_ttl_zero():
    from hyperdjango.cache import LocMemCache

    cache = LocMemCache(max_size=100)
    cache.set("key", "value", ttl=0)

    # ttl=0 expires as soon as the clock ticks past the set. Wait for the
    # EXPIRY rather than for 10ms of machine time: the regression this guards
    # (ttl=0 read as "no expiry") never expires, so it still fails once the
    # ceiling elapses, and no amount of runner load can make it pass early.
    deadline = time.monotonic() + 10.0
    result = cache.get("key")
    while result is not None and time.monotonic() < deadline:
        time.sleep(0.005)
        result = cache.get("key")
    assert result is None, f"ttl=0 should expire immediately, got: {result}"


@test("LocMemCache: ttl=None means no expiry")
def test_cache_ttl_none():
    from hyperdjango.cache import LocMemCache

    cache = LocMemCache(max_size=100)
    cache.set("key", "value", ttl=None)

    result = cache.get("key")
    assert result == "value"


@test("LocMemCache: ttl=60 keeps value alive")
def test_cache_ttl_positive():
    from hyperdjango.cache import LocMemCache

    cache = LocMemCache(max_size=100)
    cache.set("key", "value", ttl=60)

    result = cache.get("key")
    assert result == "value"


# ---------------------------------------------------------------------------
# SQL injection prevention — parameterized INTERVAL
# ---------------------------------------------------------------------------


@test("SecurityLog: parameterized INTERVAL in cleanup")
async def test_security_log_interval():
    db = Database(DB_URL)
    await db.connect()
    set_db(db)

    from hyperdjango.security import SecurityEvent, SecurityLog

    log = SecurityLog(db)
    await log.ensure_table()

    # Log an event
    await log.log(SecurityEvent.LOGIN_SUCCESS, ip="1.2.3.4", detail="test")

    # cleanup with parameterized interval should not crash
    await log.cleanup(days=90)

    # count_by_event with parameterized interval
    count = await log.count_by_event(SecurityEvent.LOGIN_SUCCESS, since_hours=24)
    assert count >= 1

    # count_by_ip with parameterized interval
    count_ip = await log.count_by_ip(
        "1.2.3.4", SecurityEvent.LOGIN_SUCCESS, since_hours=24
    )
    assert count_ip >= 1

    await db.execute("DROP TABLE IF EXISTS hyper_security_log CASCADE")
    await db.disconnect()


@test("DatabaseCache: parameterized INTERVAL in set/incr")
async def test_cache_interval():
    db = Database(DB_URL)
    await db.connect()
    set_db(db)

    from hyperdjango.cache import DatabaseCache

    await db.execute("DROP TABLE IF EXISTS hyper_cache CASCADE")

    cache = DatabaseCache(db)
    await cache.ensure_table()

    # set with parameterized interval
    await cache.set("test_key", "test_value", ttl=300)
    result = await cache.get("test_key")
    assert result == "test_value"

    # incr creates key if missing
    new_val = await cache.incr("incr_test", 5)
    assert new_val == 5

    # incr on existing incr'd key
    new_val2 = await cache.incr("incr_test", 3)
    assert new_val2 == 8

    # incr on value created by set() (JSON-encoded) — must handle quotes
    await cache.set("counter", 0, ttl=300)
    counter_val = await cache.incr("counter", 10)
    assert counter_val == 10, f"incr on set() value failed: got {counter_val}"

    # incr on string "0" set by set()
    await cache.set("str_counter", "0", ttl=300)
    str_val = await cache.incr("str_counter", 7)
    assert str_val == 7, f"incr on set('0') failed: got {str_val}"

    await db.execute("DROP TABLE IF EXISTS hyper_cache CASCADE")
    await db.disconnect()


@test("DatabaseSessionStore: parameterized INTERVAL in create/update/touch")
async def test_session_interval():
    db = Database(DB_URL)
    await db.connect()
    set_db(db)

    from hyperdjango.auth.db_sessions import DatabaseSessionStore, HyperSession
    from hyperdjango.models import create_table_for_model

    await create_table_for_model(HyperSession, db=db, drop=True)
    store = DatabaseSessionStore(max_age=3600)

    # Create session with parameterized interval
    sid = await store.create({"user_id": 1, "username": "test"})
    assert sid is not None

    # Get should work
    data = await store.get(sid)
    assert data is not None
    assert data["user_id"] == 1

    # Update with parameterized interval
    await store.update(sid, {"user_id": 1, "username": "test", "extra": "data"})
    data2 = await store.get(sid)
    assert data2["extra"] == "data"

    # Touch with parameterized interval — assert the expiry ACTUALLY advanced,
    # not merely that the session still resolves.
    before = (await HyperSession.objects.filter(session_id=sid).first()).expires_at
    assert before is not None
    # `expires_at` is `NOW() + interval` on the DATABASE's clock, so "the expiry
    # advanced" needs that clock to have ticked past the create — a condition,
    # not a 50 ms guess at how long a tick takes. Retry the touch until it does.
    # A touch that does not advance the expiry at all (the regression under
    # test) never satisfies this and still fails once the ceiling elapses.
    after = before
    deadline = time.monotonic() + 10.0
    while after <= before and time.monotonic() < deadline:
        await store.touch(sid)
        row = await HyperSession.objects.filter(session_id=sid).first()
        assert row is not None
        after = row.expires_at
    data3 = await store.get(sid)
    assert data3 is not None
    assert after is not None
    assert after > before, f"touch() did not advance expiry: {before} -> {after}"

    await db.execute("DROP TABLE IF EXISTS hyper_sessions CASCADE")
    await db.disconnect()


@test("SlowQueryLog: parameterized INTERVAL in cleanup")
async def test_slow_log_interval():
    db = Database(DB_URL)
    await db.connect()
    set_db(db)

    from hyperdjango.pool import SlowQueryLog

    log = SlowQueryLog(db, threshold_ms=100)
    await log.ensure_table()

    # Record a slow query
    await log.record("SELECT 1", 150.0)

    # Cleanup with parameterized interval should not crash
    await log.cleanup(days=7)

    # Verify the record exists
    recent = await log.get_recent(limit=10)
    assert len(recent) >= 1

    await db.execute("DROP TABLE IF EXISTS hyper_slow_queries CASCADE")
    await db.disconnect()


@test("RateLimitBackend: parameterized INTERVAL in check")
async def test_ratelimit_interval():
    db = Database(DB_URL)
    await db.connect()
    set_db(db)

    from hyperdjango.ratelimit import DatabaseRateLimitBackend

    backend = DatabaseRateLimitBackend(db)
    await backend.ensure_table()

    # check_and_increment with parameterized interval
    allowed, remaining, reset = await backend.check_and_increment(
        key="test:ip:1.2.3.4",
        max_requests=10,
        window=60,
    )
    assert allowed is True
    assert remaining == 9

    # get_usage with parameterized interval
    usage = await backend.get_usage("test:ip:1.2.3.4", window=60)
    assert usage["count"] == 1

    await db.execute("DROP TABLE IF EXISTS hyper_rate_limits CASCADE")
    await db.disconnect()


# ---------------------------------------------------------------------------
# Hash collision prevention in render_string
# ---------------------------------------------------------------------------


@test("template: render_string uses collision-resistant key")
def test_render_string_no_collision():
    import shutil
    import tempfile

    from hyperdjango.templating import TemplateEngine

    tmpdir = tempfile.mkdtemp()
    try:
        engine = TemplateEngine(tmpdir)

        # Two different strings should produce different keys
        result1 = engine.render_string("Hello {{ name }}", {"name": "Alice"})
        result2 = engine.render_string("Goodbye {{ name }}", {"name": "Alice"})

        assert result1 != result2
        assert "Hello" in result1
        assert "Goodbye" in result2
    finally:
        shutil.rmtree(tmpdir)


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------


async def main():
    tests = [
        obj
        for name, obj in globals().items()
        if callable(obj) and getattr(obj, "_is_test", False)
    ]

    print(f"\nPlatform Security Regression Tests ({len(tests)} tests)")
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
