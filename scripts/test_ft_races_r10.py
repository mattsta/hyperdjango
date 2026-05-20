"""Free-threading race regression tests (round 10).

Each test drives a tight concurrent loop over one of the four fixed sites and
asserts the multi-step invariant that used to race now holds:

  1. templating: render_string with a custom filter under concurrency ALWAYS
     applies the filter (publish-after-wire, never a naked capsule).
  2. ratelimit: the rules cache never exposes a torn (rules, index) pair — the
     compiled index always matches the rules it was built from.
  3. realtime: the ConnectionManager hook pair (callable, is_async) is never
     mismatched — is_async always agrees with iscoroutinefunction(callable).
  4. cache_adapters: ConsistentHashRing lookups during rebalance never crash and
     always return a backend from the live node set (no half-built array).

Pure-Python threads only; no native build, no full suite. Run:

    uv run python scripts/test_ft_races_r10.py
"""

# hyper-test: unit
# hyper-test-timeout: 300
#
# Spawns many OS threads with Barrier rendezvous per round — inherently
# scheduling-heavy. On a few-core runner (macOS-latest, ~3 cores) under the full
# parallel suite it stretches past the 180s pure default even after the
# per-test round counts were trimmed. The runtime is real (thread churn), not a
# hang (it passes on the beefier Linux runners), so give it budget rather than
# weaken the concurrency coverage.

from __future__ import annotations

import asyncio
import inspect
import threading
import traceback

from hyperdjango.cache_adapters import ConsistentHashRing
from hyperdjango.ratelimit import RuleBasedRateLimitMiddleware
from hyperdjango.realtime import ConnectionManager
from hyperdjango.templating import TemplateEngine
from hyperdjango.testkit import check, finish, run_main

WORKERS = 16
ROUNDS = 400
# Test 1 spawns WORKERS fresh OS threads with a full Barrier rendezvous EVERY
# round (unlike tests 2-4, which reuse a fixed worker set across ROUNDS). At
# ROUNDS=400 that is 6400 thread create/joins whose barrier requires all 16
# threads co-scheduled — it scales pathologically on a few-core CI runner
# (macOS ~3 cores) under the full parallel suite and can cross the per-file
# timeout. A smaller count still drives the compile-miss race thousands of
# times (the pre-fix bug failed within the first handful of rounds), so this
# trims wall time without weakening the race.
TEMPLATING_ROUNDS = 80


def _fail(msg: str) -> None:
    raise AssertionError(msg)


# ---------------------------------------------------------------------------
# 1. templating: publish-after-wire
# ---------------------------------------------------------------------------
def test_templating_wire_before_publish() -> None:
    engine = TemplateEngine()
    engine.add_filter("shout", lambda v: str(v).upper())

    errors: list[str] = []

    for rnd in range(TEMPLATING_ROUNDS):
        # A fresh source each round guarantees a compile-miss that every worker
        # races on simultaneously. Pre-fix, a worker could `get` the capsule the
        # winner published BEFORE wiring the "shout" filter into it.
        source = "{{ msg|shout }}" + "{#" + str(rnd) + "#}"
        barrier = threading.Barrier(WORKERS)

        def work() -> None:
            barrier.wait()
            try:
                out = engine.render_string(source, {"msg": "hello"})
            except Exception as exc:  # noqa: BLE001 - record, don't swallow silently
                errors.append(f"render raised: {exc!r}")
                return
            if out != "HELLO":
                errors.append(f"filter not applied: {out!r}")

        threads = [threading.Thread(target=work) for _ in range(WORKERS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if errors:
            _fail(errors[0])


# ---------------------------------------------------------------------------
# 2. ratelimit: atomic (rules, index, time) state
# ---------------------------------------------------------------------------
class _FakeDB:
    """Async DB stub returning a variable-length rule set per query."""

    def __init__(self) -> None:
        self._n = 0

    async def query(self, sql: str, *args: object) -> list[dict[str, object]]:
        await asyncio.sleep(0)  # yield so concurrent loaders interleave
        self._n = (self._n % 5) + 1
        n = self._n
        return [
            {
                "id": i,
                "name": f"rule{i}",
                "path_pattern": f"/api/{i}/*",
                "method": "GET",
                "tier": "free",
                "max_requests": 100,
                "window_seconds": 60,
                "cost": 1,
                "priority": i,
                "is_active": True,
            }
            for i in range(n)
        ]


def test_ratelimit_no_torn_rules_index() -> None:
    async def run() -> None:
        mw = RuleBasedRateLimitMiddleware(
            tiers={"free": {"max_requests": 100, "window": 60}},
            db=_FakeDB(),
            rules_cache_ttl=0,  # every call is stale → forces reload contention
        )
        errors: list[str] = []
        stop = {"v": False}

        async def reader() -> None:
            while not stop["v"]:
                state = mw._rules_state  # single-reference snapshot
                if state is not None:
                    rules, index, _ = state
                    if index is None:
                        errors.append("rules present but index is None (torn)")
                    elif index._rule_count != len(rules):
                        errors.append(
                            f"index/rules mismatch: {index._rule_count} != {len(rules)}"
                        )
                await asyncio.sleep(0)

        async def loader() -> None:
            for i in range(ROUNDS):
                await mw._ensure_rules_loaded()
                if i % 7 == 0:
                    mw.clear_rules_cache()
                await asyncio.sleep(0)

        readers = [asyncio.create_task(reader()) for _ in range(4)]
        loaders = [asyncio.create_task(loader()) for _ in range(WORKERS)]
        await asyncio.gather(*loaders)
        stop["v"] = True
        await asyncio.gather(*readers)
        if errors:
            _fail(errors[0])

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 3. realtime: hook (callable, is_async) tuple
# ---------------------------------------------------------------------------
def test_realtime_hook_pair_atomic() -> None:
    mgr = ConnectionManager(layer=None)  # layer unused by the hook fields

    async def async_hook(info: object) -> None:  # noqa: RUF029
        return None

    def sync_hook(info: object) -> None:
        return None

    errors: list[str] = []
    stop = threading.Event()

    def writer() -> None:
        i = 0
        while not stop.is_set():
            mgr.on_connect = async_hook if (i & 1) else sync_hook
            mgr.on_disconnect = sync_hook if (i & 1) else async_hook
            i += 1

    def reader() -> None:
        while not stop.is_set():
            for hook in (mgr._on_connect_hook, mgr._on_disconnect_hook):
                if hook is None:
                    continue
                fn, is_async = hook
                expect = inspect.iscoroutinefunction(fn)
                if is_async != expect:
                    errors.append(
                        f"hook pair mismatched: is_async={is_async} but "
                        f"iscoroutinefunction={expect}"
                    )

    threads = [threading.Thread(target=writer) for _ in range(4)]
    threads += [threading.Thread(target=reader) for _ in range(WORKERS)]
    for t in threads:
        t.start()
    import time as _t

    _t.sleep(1.0)
    stop.set()
    for t in threads:
        t.join()
    if errors:
        _fail(errors[0])


# ---------------------------------------------------------------------------
# 4. cache_adapters: ConsistentHashRing rebalance
# ---------------------------------------------------------------------------
def test_ring_rebalance_stays_in_set() -> None:
    # Distinct sentinel backends; the ring stores + returns the instances.
    universe = {f"n{i}": object() for i in range(8)}
    ring = ConsistentHashRing(nodes={f"n{i}": universe[f"n{i}"] for i in range(3)})

    known = set(universe.values())
    errors: list[str] = []
    stop = threading.Event()

    def rebalancer() -> None:
        i = 3
        while not stop.is_set():
            name = f"n{i % 8}"
            if i % 2 == 0:
                ring.add_node(name, universe[name])
            else:
                ring.remove_node(name)
            i += 1

    def looker() -> None:
        k = 0
        while not stop.is_set():
            try:
                node = ring.get_node(f"user:{k}")
            except RuntimeError:
                # Legitimate: ring momentarily emptied by concurrent removes.
                k += 1
                continue
            except Exception as exc:  # noqa: BLE001
                errors.append(f"get_node raised: {exc!r}")
                return
            if node is not None and node not in known:
                errors.append("get_node returned an unknown backend (torn build)")
                return
            k += 1

    threads = [threading.Thread(target=rebalancer) for _ in range(3)]
    threads += [threading.Thread(target=looker) for _ in range(WORKERS)]
    for t in threads:
        t.start()
    import time as _t

    _t.sleep(1.0)
    stop.set()
    for t in threads:
        t.join()
    if errors:
        _fail(errors[0])


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

# One counted check per race site: the loop body raises (via ``_fail``) on the
# first observed violation, so the run still aborts there — the tally is just
# emitted before exiting.
_TESTS = [
    (
        test_templating_wire_before_publish,
        f"[1] templating: {TEMPLATING_ROUNDS * WORKERS} concurrent renders,"
        " filter always applied",
    ),
    (
        test_ratelimit_no_torn_rules_index,
        "[2] ratelimit: rules/index never torn across concurrent reload + clear",
    ),
    (
        test_realtime_hook_pair_atomic,
        "[3] realtime: hook (callable, is_async) pair never mismatched",
    ),
    (
        test_ring_rebalance_stays_in_set,
        "[4] cache_adapters: ring lookups during rebalance stay in-set, no crash",
    ),
]


def main() -> bool:
    print("FT race regression (round 10):")
    for fn, label in _TESTS:
        try:
            fn()
        except Exception as exc:
            check(label, False, f"{type(exc).__name__}: {exc}")
            traceback.print_exc()
            finish()
            return False
        check(label, True)
    print()
    return finish()


if __name__ == "__main__":
    run_main(main)
