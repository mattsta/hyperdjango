"""Gate: native lock primitives must be REAL mutual exclusion, not no-ops.

Closes the "silently-broken lock primitive" class. On macOS,
`std.mem.zeroes(pthread_rwlock_t)` clobbered the platform static-initializer
magic (`sig`), so every `pthread_rwlock_wrlock`/`rdlock` returned EINVAL and
acquired NOTHING — every `py.RwLock` silently became a no-op. Under
free-threading (3.14t) that turned every lock-guarded shared structure (metric
label maps, the response cache, the consistent-hash ring) into an
unsynchronized data race → lost updates → out-of-bounds reads → SIGSEGV.

These vectors would all FAIL against that broken build and PASS against a
correct one:

  1. `_test_rwlock_stress` / `_test_mutex_stress` — native Zig threads each do
     `lock(); counter += 1; unlock()` (a non-atomic RMW). A correct lock
     serializes every increment → counter == n_threads*iters EXACTLY. A no-op
     lock drops updates → counter < n_threads*iters. This is the direct witness
     for the macOS RwLock-no-op finding.

  2. A production `_hashring_*` ring under W reader threads calling get_node in
     a tight loop while M mutator threads remove/re-add+rebuild. Every returned
     node name must be a real registered name. A no-op RwLock lets a reader copy
     out of `name_buf` / index `node_indices[]` while a mutator reallocs+rewrites
     them → a torn/dangling name that is NOT in the registered universe, or a
     crash.

Everything here is pure-native and needs the compiled extension; the whole
module skips if it (or the new test exports) is absent. It passes under a GIL
build too — it simply cannot fail there — so it is safe in normal CI.
"""

import random
import threading

import pytest

_native = pytest.importorskip("hyperdjango._hyperdjango_native")

# Bounded so the gate is fast (<~1s) yet the race window is wide. 8 threads x
# 100k RMWs = 800k contended increments — a no-op lock loses thousands.
STRESS_THREADS = 8
STRESS_ITERS = 100_000


# ---------------------------------------------------------------------------
# 1. Native lock stressors — the direct no-op witness
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not hasattr(_native, "_test_rwlock_stress"),
    reason="native build predates _test_rwlock_stress export (needs rebuild)",
)
def test_rwlock_is_not_a_noop():
    expected = STRESS_THREADS * STRESS_ITERS
    got = _native._test_rwlock_stress(STRESS_THREADS, STRESS_ITERS)
    assert got == expected, (
        f"py.RwLock lost updates: {got} != {expected} "
        f"({expected - got} increments dropped) — the write lock is a no-op"
    )


@pytest.mark.skipif(
    not hasattr(_native, "_test_mutex_stress"),
    reason="native build predates _test_mutex_stress export (needs rebuild)",
)
def test_mutex_is_not_a_noop():
    expected = STRESS_THREADS * STRESS_ITERS
    got = _native._test_mutex_stress(STRESS_THREADS, STRESS_ITERS)
    assert got == expected, (
        f"py.Mutex lost updates: {got} != {expected} "
        f"({expected - got} increments dropped) — the mutex is a no-op"
    )


@pytest.mark.skipif(
    not hasattr(_native, "_test_rwlock_stress"),
    reason="native build predates _test_rwlock_stress export (needs rebuild)",
)
def test_rwlock_single_thread_baseline_is_exact():
    # A single thread must always reach the exact count regardless of locking —
    # guards against the stressor itself miscounting (spawn failure, arg bug).
    assert _native._test_rwlock_stress(1, 50_000) == 50_000


# ---------------------------------------------------------------------------
# 2. Production lock under load — the consistent-hash ring
# ---------------------------------------------------------------------------


def _drive_hashring():
    """W readers hammer get_node while M mutators churn+rebuild the ring.

    Uses the raw `_hashring_*` FFI so the invariant isolates the Zig RwLock
    (no Python-dict layer in between). CORE nodes are never removed, so a
    correctly-locked ring ALWAYS returns some valid registered name; CHURN
    nodes are repeatedly removed and re-added under the write lock. Every name
    a reader observes must be in the registered universe — a no-op RwLock
    yields a torn/dangling name (not in the universe) or crashes the process.
    """
    handle = _native._hashring_new(4, 40)

    core = [f"core-{i}" for i in range(8)]
    churn = [f"churn-{i}" for i in range(8)]
    universe = frozenset(core + churn)

    for name in core + churn:
        # add_node(handle, name, weight, vnodes, instance)
        _native._hashring_add_node(handle, name, 1, 0, None)
    _native._hashring_build(handle)

    keys = [f"key-{i}" for i in range(512)]
    stop = threading.Event()
    bad: list[str] = []
    bad_lock = threading.Lock()
    ready = threading.Barrier(4 + 2)

    def reader():
        ready.wait()
        rnd = random.Random(1234)
        while not stop.is_set():
            for _ in range(64):
                name = _native._hashring_get_node(handle, rnd.choice(keys))
                # Core nodes are always present+built, so a correct ring never
                # returns None; and any name returned must be a real one.
                if name is not None and name not in universe:
                    with bad_lock:
                        if len(bad) < 16:
                            bad.append(name)

    def mutator(seed):
        ready.wait()
        rnd = random.Random(seed)
        for _ in range(400):
            victim = rnd.choice(churn)
            _native._hashring_remove_node(handle, victim)  # rebuilds internally
            _native._hashring_add_node(handle, victim, 1, 0, None)
            _native._hashring_build(handle)

    readers = [threading.Thread(target=reader) for _ in range(4)]
    mutators = [threading.Thread(target=mutator, args=(i,)) for i in range(2)]
    for t in readers + mutators:
        t.start()
    for t in mutators:
        t.join()
    stop.set()
    for t in readers:
        t.join()

    _native._hashring_free(handle)
    return bad


@pytest.mark.skipif(
    not hasattr(_native, "_hashring_new"),
    reason="native build lacks the hashring FFI",
)
def test_hashring_returns_only_registered_nodes_under_churn():
    bad = _drive_hashring()
    assert bad == [], (
        f"hashring returned out-of-set node names under concurrent churn "
        f"(RwLock no-op → torn/dangling reads): {bad}"
    )
