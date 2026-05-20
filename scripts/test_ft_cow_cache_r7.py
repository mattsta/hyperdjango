#!/usr/bin/env python3
"""Free-threading / COW regression tests (round 7).

Proves the fixes for the guard-before-data + no-op-cache bug class:

1. get_cache() memoizes its fallback — returns the SAME LocMemCache across
   calls, so the default @cached / get-set path actually caches (set then get
   hits) instead of silently discarding a fresh cache each call.
2. cache._cache_namespace() publishes (key, value) as ONE atomic tuple — a
   concurrent reader never sees a torn (old-key, new-value) pair.
3. VersionMiddleware caches (raw, header, inject) as one atomic tuple — every
   observed snapshot is internally consistent (guard paired with its data).
4. TaskScheduler.start() is lock-guarded — calling it concurrently spawns
   exactly ONE scheduler thread (no unlocked double-spawn → jobs firing twice).

Pure-Python, uses the existing .so. No database required.

Run: uv run hyper-test ft_cow_cache_r7   (or: python scripts/test_ft_cow_cache_r7.py)
"""

# hyper-test: unit

import sys
import threading

passed = 0
failed = 0


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        print(f"  PASS: {name}")
        passed += 1
    else:
        print(f"  FAIL: {name} — {detail}")
        failed += 1


def test_get_cache_memoized():
    """get_cache() returns the SAME instance and the default cache actually caches."""
    print("\n=== get_cache() memoized fallback ===")
    import hyperdjango.cache as cachemod
    from hyperdjango.cache import LocMemCache, cached, get_cache

    # Force the "no explicit cache configured" state under the module lock.
    with cachemod._default_cache_lock:
        cachemod._default_cache = None

    c1 = get_cache()
    c2 = get_cache()
    check("get_cache() returns a LocMemCache", isinstance(c1, LocMemCache))
    check("get_cache() memoized (same instance across calls)", c1 is c2)

    # Concurrent first-callers all see one shared instance (no per-call rebuild).
    with cachemod._default_cache_lock:
        cachemod._default_cache = None
    seen: list[object] = []
    barrier = threading.Barrier(16)

    def grab():
        barrier.wait()
        seen.append(get_cache())

    threads = [threading.Thread(target=grab) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check(
        "concurrent get_cache() all share one instance",
        len({id(x) for x in seen}) == 1,
        f"distinct instances: {len({id(x) for x in seen})}",
    )

    # The default @cached path must actually cache: a set then get hits.
    calls = {"n": 0}

    @cached(ttl=60)
    def compute(x):
        calls["n"] += 1
        return x * 2

    r1 = compute(21)
    r2 = compute(21)
    check("@cached returns correct value", r1 == 42 and r2 == 42)
    check(
        "@cached default cache actually caches (fn body ran once)",
        calls["n"] == 1,
        f"body ran {calls['n']} times",
    )


def test_namespace_atomic_pair():
    """_cache_namespace() never publishes a torn (key, value) pair."""
    print("\n=== _cache_namespace() atomic (key, value) ===")
    import hyperdjango.cache as cachemod
    from hyperdjango.cache import _cache_namespace, make_cache_key
    from hyperdjango.conf import DEFAULTS, get_setting  # noqa: F401

    # Functional: namespace reflects current prefix/version.
    cachemod._invalidate_namespace_cache()
    ns = _cache_namespace()
    check("namespace resolves to a non-empty string", bool(ns))
    check("make_cache_key applies namespace", make_cache_key("k") == ns + "k")

    # Concurrency: churn CACHE_VERSION while readers hammer _cache_namespace,
    # then assert the published memo is internally consistent — the value can
    # ALWAYS be regenerated from its own key. A torn (old-key, new-value) pair
    # (the pre-fix bug) could never satisfy this.
    orig_version = get_setting("CACHE_VERSION")
    orig_prefix = get_setting("CACHE_KEY_PREFIX")
    stop = threading.Event()
    torn = {"hit": False}

    def writer():
        i = 0
        while not stop.is_set():
            DEFAULTS["CACHE_VERSION"] = str(i % 7)
            DEFAULTS["CACHE_KEY_PREFIX"] = "p" if i % 2 else ""
            cachemod._invalidate_namespace_cache()
            i += 1

    def reader():
        while not stop.is_set():
            _cache_namespace()
            memo = cachemod._namespace_memo
            if memo is not None:
                (prefix, version), value = memo
                expected = f"{prefix}:v{version}:" if prefix else f"v{version}:"
                if value != expected:
                    torn["hit"] = True

    threads = [threading.Thread(target=writer) for _ in range(2)]
    threads += [threading.Thread(target=reader) for _ in range(6)]
    for t in threads:
        t.start()
    # Spin briefly under contention.
    for _ in range(200000):
        _cache_namespace()
    stop.set()
    for t in threads:
        t.join()

    # Restore settings.
    DEFAULTS["CACHE_VERSION"] = orig_version
    DEFAULTS["CACHE_KEY_PREFIX"] = orig_prefix
    cachemod._invalidate_namespace_cache()

    check("no torn (key, value) pair observed under concurrency", not torn["hit"])


def test_appversion_atomic_snapshot():
    """VersionMiddleware snapshot pairs the guard with its data atomically."""
    print("\n=== VersionMiddleware atomic snapshot ===")
    from hyperdjango.response import _sanitize_header
    from hyperdjango.standalone_middleware import VersionMiddleware

    mw = VersionMiddleware()

    # Functional: refresh builds a consistent snapshot.
    snap = mw._refresh_cache("1.2.3")
    check(
        "refresh returns consistent snapshot",
        snap[0] == "1.2.3" and snap[1] == _sanitize_header("1.2.3"),
    )
    check("snapshot stored on instance", mw._cache_snapshot is snap)

    # Concurrency: writers refresh with different versions while readers read
    # the snapshot; every observed snapshot must have header == sanitize(raw)
    # (guard travels WITH its data — never a new guard beside stale data).
    stop = threading.Event()
    torn = {"hit": False}

    def writer(base):
        i = 0
        while not stop.is_set():
            mw._refresh_cache(f"{base}.{i % 97}")
            i += 1

    def reader():
        while not stop.is_set():
            s = mw._cache_snapshot
            raw, header, _inject = s
            if header != _sanitize_header(raw):
                torn["hit"] = True

    threads = [threading.Thread(target=writer, args=(b,)) for b in ("9", "8", "7")]
    threads += [threading.Thread(target=reader) for _ in range(6)]
    for t in threads:
        t.start()
    for _ in range(100000):
        s = mw._cache_snapshot
        if s[1] != _sanitize_header(s[0]):
            torn["hit"] = True
    stop.set()
    for t in threads:
        t.join()

    check("no torn (guard, data) snapshot observed under concurrency", not torn["hit"])


def test_scheduler_single_spawn():
    """TaskScheduler.start() called concurrently spawns exactly one thread."""
    print("\n=== TaskScheduler.start() single spawn ===")
    from hyperdjango.tasks import TaskScheduler

    def count_scheduler_threads():
        return sum(1 for t in threading.enumerate() if t.name == "task-scheduler")

    baseline = count_scheduler_threads()
    sched = TaskScheduler()

    barrier = threading.Barrier(24)

    def racer():
        barrier.wait()
        sched.start()

    threads = [threading.Thread(target=racer) for _ in range(24)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    spawned = count_scheduler_threads() - baseline
    check(
        "concurrent start() spawns exactly one scheduler thread",
        spawned == 1,
        f"spawned {spawned}",
    )

    sched.stop()
    check("stop() joins the scheduler thread", count_scheduler_threads() == baseline)


def main():
    test_get_cache_memoized()
    test_namespace_atomic_pair()
    test_appversion_atomic_snapshot()
    test_scheduler_single_spawn()

    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed == 0:
        print("All free-threading COW/cache tests passed!")
    else:
        print(f"{failed} tests need attention")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
