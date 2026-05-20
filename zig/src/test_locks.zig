// test_locks.zig — TEST-ONLY native lock-correctness stressors.
//
// These functions exist solely to prove that py.RwLock / py.Mutex are REAL
// mutual-exclusion primitives and not silently-degraded no-ops. They are
// registered in the module method table under `_test_rwlock_stress` /
// `_test_mutex_stress` and are driven by tests/test_freethread_lock_correctness.py.
//
// Why this gate exists: on macOS, `std.mem.zeroes(pthread_rwlock_t)` clobbered
// the platform's static-initializer magic (`sig`), so every wrlock/rdlock
// returned EINVAL and acquired NOTHING — every py.RwLock became a no-op. Under
// free-threading that turned guarded shared state (metric label maps, response
// cache, the hashring) into unsynchronized data races → lost updates → SIGSEGV.
// A plain (non-atomic) `g_counter += 1` under a WRITE lock is the canonical
// witness: a correct lock serializes every read-modify-write so the final count
// is EXACTLY n_threads * iters; a no-op lock drops updates → strictly less.
//
// Each worker is pure Zig and never touches the Python C-API, so we drop the
// GIL around the spin: workers then run with genuine OS-level parallelism (even
// on a GIL build) which maximally exposes the race window, and other Python
// threads are not blocked while we join.

const std = @import("std");
const py = @import("py.zig");
const c = py.c;

const MAX_THREADS = 256;

// ── RwLock (WRITE-lock) stressor ────────────────────────────────────────────
var g_rwlock: py.RwLock = .{};
var g_rwlock_counter: u64 = 0;

fn rwlockWorker(iters: u64) void {
    var i: u64 = 0;
    while (i < iters) : (i += 1) {
        g_rwlock.lock();
        // Deliberately NON-atomic read-modify-write: correctness depends
        // entirely on the surrounding lock actually excluding other writers.
        g_rwlock_counter += 1;
        g_rwlock.unlock();
    }
}

// ── Mutex stressor ──────────────────────────────────────────────────────────
var g_mutex: py.Mutex = .{};
var g_mutex_counter: u64 = 0;

fn mutexWorker(iters: u64) void {
    var i: u64 = 0;
    while (i < iters) : (i += 1) {
        g_mutex.lock();
        g_mutex_counter += 1;
        g_mutex.unlock();
    }
}

fn runStress(
    args: ?*c.PyObject,
    counter: *u64,
    comptime worker: anytype,
) ?*c.PyObject {
    var n_threads: c_long = 0;
    var iters: c_long = 0;
    if (c.PyArg_ParseTuple(args, "ll", &n_threads, &iters) == 0) return null;
    if (n_threads < 1 or n_threads > MAX_THREADS or iters < 0) {
        c.PyErr_SetString(c.PyExc_ValueError, "n_threads in 1..256, iters >= 0");
        return null;
    }

    const n: usize = @intCast(n_threads);
    const it: u64 = @intCast(iters);
    counter.* = 0;

    var threads: [MAX_THREADS]std.Thread = undefined;
    var spawned: usize = 0;

    // Drop the GIL: workers are pure-Zig, so this yields true parallelism and
    // lets other Python threads progress while we block in join().
    const save = py.PyEval_SaveThread();
    while (spawned < n) : (spawned += 1) {
        threads[spawned] = std.Thread.spawn(.{}, worker, .{it}) catch break;
    }
    for (threads[0..spawned]) |th| th.join();
    py.PyEval_RestoreThread(save);

    if (spawned != n) {
        c.PyErr_SetString(c.PyExc_RuntimeError, "test stressor: thread spawn failed");
        return null;
    }
    return c.PyLong_FromUnsignedLongLong(counter.*);
}

/// _test_rwlock_stress(n_threads: int, iters: int) -> int
/// Spins n_threads real Zig threads each doing `iters` WRITE-locked increments
/// of a shared counter. Correct lock -> n_threads*iters; no-op -> strictly less.
pub fn test_rwlock_stress(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    return runStress(args, &g_rwlock_counter, rwlockWorker);
}

/// _test_mutex_stress(n_threads: int, iters: int) -> int
/// Same contract as _test_rwlock_stress but exercises py.Mutex.
pub fn test_mutex_stress(_: ?*c.PyObject, args: ?*c.PyObject) callconv(.c) ?*c.PyObject {
    return runStress(args, &g_mutex_counter, mutexWorker);
}
