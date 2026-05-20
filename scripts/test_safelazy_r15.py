#!/usr/bin/env python3
# hyper-test: unit
"""SafeLazy — the one thread-safe lazy-singleton primitive (round-15, C6)."""

import sys
import threading

from hyperdjango._lazy import SafeLazy

_p = _f = 0


def check(name, cond, detail=""):
    global _p, _f
    if cond:
        _p += 1
    else:
        _f += 1
        print(f"  FAIL {name} — {detail}")


def main():
    # build-once under heavy concurrency (the free-threading race the primitive exists to prevent)
    builds = []
    build_lock = threading.Lock()

    def factory():
        with build_lock:
            builds.append(1)
        return object()

    lazy = SafeLazy(factory)
    results = [None] * 64
    barrier = threading.Barrier(64)

    def worker(i):
        barrier.wait()  # maximize the race window
        results[i] = lazy.get()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(64)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    check(
        "factory ran EXACTLY once under 64-thread race",
        len(builds) == 1,
        f"ran {len(builds)}x",
    )
    first = results[0]
    check("all callers got the SAME instance", all(r is first for r in results))
    check("get() after build returns the same value", lazy.get() is first)

    # peek / built
    check("built is True after get", lazy.built)
    check("peek returns the value", lazy.peek() is first)

    # not-built state
    lazy2 = SafeLazy(lambda: 42)
    check("built is False before get", not lazy2.built)
    check(
        "peek returns None before build (no build triggered)",
        lazy2.peek() is None and not lazy2.built,
    )
    check("get builds", lazy2.get() == 42 and lazy2.built)

    # reset rebuilds
    seq = SafeLazy(lambda i=[0]: (i.__setitem__(0, i[0] + 1), i[0])[1])
    a = seq.get()
    check("cached across calls", seq.get() == a)
    seq.reset()
    check("reset -> not built", not seq.built)
    b = seq.get()
    check("rebuilds after reset (new value)", b == a + 1)

    print(f"{_p} passed, {_f} failed")
    return 1 if _f else 0


if __name__ == "__main__":
    sys.exit(main())
