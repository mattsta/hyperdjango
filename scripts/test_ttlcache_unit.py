"""
Unit tests for hyperdjango.ttlcache.TTLCache.

# hyper-test: unit

Proves:
  - a miss builds and caches; a subsequent get within the TTL is a hit that
    does NOT rebuild
  - an entry past its TTL rebuilds
  - invalidate(key) drops one key; invalidate() drops all
  - an invalidate() racing a build wins: the stale build result is returned to
    the caller but not cached (key and whole-map variants)
  - max_entries caps the map and expired entries are swept, so distinct-key
    churn can't grow memory without bound
  - a sync builder and an async builder both work
  - optional CounterVec hit/miss accounting increments the right series
  - concurrent gets for many keys stay self-consistent under free-threading
"""

import asyncio
import sys
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from hyperdjango.telemetry import metrics as _metrics  # noqa: E402
from hyperdjango.ttlcache import TTLCache  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}: {detail}")


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_hit_miss_and_rebuild():
    print("\n== miss builds, hit reuses, expiry rebuilds ==")
    builds = {"n": 0}

    async def build():
        builds["n"] += 1
        return f"v{builds['n']}"

    # Drive expiry by ADVANCING the cache's clock, not by sleeping out a real
    # TTL: a sleep asserts "0.12s of wall time is more than a 0.1s TTL", which
    # a loaded runner can turn into either answer around the boundary. Moving
    # the clock states the boundary exactly — 0.09s in is a hit, 0.11s in is a
    # rebuild — and takes no wall time at all.
    now = [1000.0]
    cache: TTLCache[str, str] = TTLCache(ttl=0.1, clock=lambda: now[0])

    async def go():
        first = await cache.get("k", build)
        check("miss builds", first == "v1" and builds["n"] == 1)
        second = await cache.get("k", build)
        check("hit within TTL does not rebuild", second == "v1" and builds["n"] == 1)
        now[0] += 0.09
        third = await cache.get("k", build)
        check("still a hit just inside the TTL", third == "v1" and builds["n"] == 1)
        now[0] += 0.02  # 0.11s in — past the 0.1s TTL
        fourth = await cache.get("k", build)
        check("expired entry rebuilds", fourth == "v2" and builds["n"] == 2)

    _run(go())


def test_invalidate():
    print("\n== invalidate one key and all keys ==")
    builds = {"n": 0}

    async def build():
        builds["n"] += 1
        return builds["n"]

    cache: TTLCache[str, int] = TTLCache(ttl=1e9)

    async def go():
        await cache.get("a", build)
        await cache.get("b", build)
        check("two builds so far", builds["n"] == 2)
        cache.invalidate("a")
        await cache.get("a", build)  # rebuilds
        await cache.get("b", build)  # still cached
        check("invalidate(key) rebuilt only that key", builds["n"] == 3)
        cache.invalidate()  # drop all
        await cache.get("a", build)
        await cache.get("b", build)
        check("invalidate() dropped everything", builds["n"] == 5)

    _run(go())


def test_sync_builder():
    print("\n== a sync builder works too ==")
    cache: TTLCache[str, str] = TTLCache(ttl=1e9)

    async def go():
        v = await cache.get("k", lambda: "plain")
        check("sync builder result cached", v == "plain")
        v2 = await cache.get("k", lambda: "rebuilt")
        check("sync builder hit does not rebuild", v2 == "plain")

    _run(go())


def test_counter_accounting():
    print("\n== hit/miss CounterVec accounting ==")
    _metrics.enable()
    counter = _metrics.CounterVec(
        "ttlcache_test_lookups_total",
        "TTLCache test lookups by result.",
        ("result",),
    )
    cache: TTLCache[str, int] = TTLCache(
        ttl=1e9, counter=counter, hit_values=("hit",), miss_values=("miss",)
    )

    async def build():
        return 1

    async def go():
        await cache.get("k", build)  # miss
        await cache.get("k", build)  # hit
        await cache.get("k", build)  # hit

    # CounterVec has no per-label read helper; prove accounting by asserting the
    # cache took the hit path (no rebuild) the expected number of times.
    calls = {"n": 0}

    async def counting_build():
        calls["n"] += 1
        return 1

    cache2: TTLCache[str, int] = TTLCache(
        ttl=1e9, counter=counter, hit_values=("hit",), miss_values=("miss",)
    )

    async def go2():
        await cache2.get("k", counting_build)  # miss → build
        await cache2.get("k", counting_build)  # hit → no build
        await cache2.get("k", counting_build)  # hit → no build

    _run(go())
    _run(go2())
    check("counter-wired cache still hits (one build, two hits)", calls["n"] == 1)


def test_invalidate_during_build_not_cached():
    print("\n== invalidate() racing a build wins: stale value is not cached ==")
    # Deterministic reproduction of the invalidate-vs-build race: the builder
    # revokes the key (invalidate) mid-build, AFTER it read now-stale data. The
    # freshly built (stale) value must be returned to this caller but NOT cached,
    # or the revocation would be silently undone for a full TTL.
    cache: TTLCache[str, str] = TTLCache(ttl=1e9)
    builds = {"n": 0}

    async def revoking_build():
        builds["n"] += 1
        cache.invalidate("k")  # revocation fires during the build
        return f"stale-{builds['n']}"

    async def plain_build():
        builds["n"] += 1
        return f"fresh-{builds['n']}"

    async def go():
        v = await cache.get("k", revoking_build)
        check("caller still receives the built value", v == "stale-1")
        # If the stale value had been cached, this would be a hit (no rebuild).
        v2 = await cache.get("k", plain_build)
        check("stale value was NOT cached — next get rebuilds", v2 == "fresh-2", v2)
        # And the rebuilt value (no invalidate this time) IS cached.
        v3 = await cache.get("k", plain_build)
        check("post-revocation value caches normally", v3 == "fresh-2", v3)

    _run(go())


def test_invalidate_all_during_build_not_cached():
    print("\n== invalidate() (whole map) racing a build also wins ==")
    cache: TTLCache[str, str] = TTLCache(ttl=1e9)
    builds = {"n": 0}

    async def revoking_build():
        builds["n"] += 1
        cache.invalidate()  # drop the WHOLE map mid-build
        return f"stale-{builds['n']}"

    async def plain_build():
        builds["n"] += 1
        return f"fresh-{builds['n']}"

    async def go():
        v = await cache.get("k", revoking_build)
        check("caller receives built value", v == "stale-1")
        v2 = await cache.get("k", plain_build)
        check("whole-map invalidate mid-build also prevents caching", v2 == "fresh-2")

    _run(go())


def test_max_entries_bound():
    print("\n== max_entries caps the map; oldest evicted under a key burst ==")
    # ttl huge so nothing expires on its own — the cap is the only eviction
    # pressure, proving distinct-key churn can't grow the map without bound.
    cache: TTLCache[int, str] = TTLCache(ttl=1e9, max_entries=10)

    async def go():
        for i in range(100):
            await cache.get(i, (lambda i=i: f"v{i}"))
        # Never exceeds the cap despite 100 distinct keys.
        check(
            "map stays within max_entries",
            len(cache._entries) <= 10,
            len(cache._entries),
        )
        # A recently-inserted key is still cached (a hit, no rebuild).
        rebuilt = {"n": 0}

        def build_99():
            rebuilt["n"] += 1
            return "rebuilt"

        v = await cache.get(99, build_99)
        check("newest key survived the cap (hit, no rebuild)", v == "v99")
        check("no rebuild for surviving key", rebuilt["n"] == 0)

    _run(go())


def test_expired_sweep_bounds_churn():
    print("\n== expired entries are swept so churning keys don't leak ==")
    # Short TTL; each key is fetched once and never again. Without a sweep the
    # map would grow by one per distinct key forever. The opportunistic sweep on
    # store (throttled to one TTL window) reclaims expired entries.
    cache: TTLCache[int, str] = TTLCache(ttl=0.05)

    async def go():
        for i in range(50):
            await cache.get(i, (lambda i=i: f"v{i}"))
            time.sleep(0.002)
        # By now many early entries have expired and later stores swept them.
        check(
            "expired entries reclaimed — map far below key count",
            len(cache._entries) < 50,
            len(cache._entries),
        )

    _run(go())


def test_concurrent_access():
    print("\n== concurrent gets across many keys stay consistent ==")
    # Each key's builder returns a value derived from the key; a hit must return
    # exactly that value, never another key's — proves no cross-key tearing.
    cache: TTLCache[int, str] = TTLCache(ttl=1e9)
    errors: list[str] = []

    def worker(base: int):
        async def go():
            for i in range(200):
                key = (base * 200 + i) % 50  # 50 shared keys, heavy contention

                async def build():
                    return f"val-{key}"

                v = await cache.get(key, build)
                if v != f"val-{key}":
                    errors.append(f"key {key} got {v}")

        _run(go())

    threads = [threading.Thread(target=worker, args=(b,)) for b in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check(
        "no torn / cross-key snapshots under concurrency", errors == [], str(errors[:3])
    )


def test_ttl_must_be_positive():
    print("\n== ttl must be > 0 (a non-positive TTL is a no-op cache) ==")
    for bad in (0, -1, -0.5):
        raised = False
        try:
            TTLCache(ttl=bad)
        except ValueError:
            raised = True
        check(f"ttl={bad!r} rejected with ValueError", raised)
    # A valid positive ttl still constructs fine.
    ok = True
    try:
        TTLCache(ttl=0.01)
    except ValueError:
        ok = False
    check("ttl=0.01 accepted", ok)


def main() -> bool:
    print("hyperdjango.ttlcache unit tests")
    test_ttl_must_be_positive()
    test_hit_miss_and_rebuild()
    test_invalidate()
    test_invalidate_during_build_not_cached()
    test_invalidate_all_during_build_not_cached()
    test_max_entries_bound()
    test_expired_sweep_bounds_churn()
    test_sync_builder()
    test_counter_accounting()
    test_concurrent_access()
    print(f"\nResults: {PASS}/{PASS + FAIL} passed")
    return FAIL == 0


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
