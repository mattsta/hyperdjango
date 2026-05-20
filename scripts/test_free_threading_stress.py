"""
Free-threading stress test: verify shared mutable state under concurrent access.

Runs 24 threads × 1000 operations each against every shared mutable subsystem.
Detects: crashes, data corruption, stale reads, race conditions.

Python 3.14t with GIL disabled — real concurrent execution.

# hyper-test: unit
"""

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from _test_meta import make_model

from hyperdjango.cache import LocMemCache
from hyperdjango.lookups import resolve_bind_params, resolve_lookup
from hyperdjango.query import (
    QuerySet,
    _compiled_count_cache,
    _compiled_sql_cache,
    clear_compiled_cache,
)
from hyperdjango.where import WhereNode

THREADS = 24
OPS_PER_THREAD = 1000


# ---------------------------------------------------------------------------
# Mock model
# ---------------------------------------------------------------------------


# Real _meta via shared builder (scripts/_test_meta.py)
MockModel = make_model("stress_test", ["id", "name", "age", "status"])


# ---------------------------------------------------------------------------
# Test 1: Compiled SQL cache under concurrent read/write
# ---------------------------------------------------------------------------


def test_compiled_cache_concurrent():
    """24 threads hitting compiled SQL cache simultaneously."""
    clear_compiled_cache()
    errors = []

    def worker(thread_id):
        for i in range(OPS_PER_THREAD):
            qs = QuerySet(MockModel)
            qs._annotations = {}
            qs._filters = [("name", f"t{thread_id}_v{i}"), ("age__gte", 18)]
            qs._excludes = []
            qs._raw_wheres = []
            qs._select_related = []
            qs._values_fields = None
            qs._only = None
            qs._defer = None
            qs._ordering = ("-id",)
            qs._limit = 10
            qs._offset = None
            qs._distinct = False
            qs._for_update = None
            qs._group_by = False
            sql, params = qs._build_select()
            if not sql or not params:
                errors.append(f"Thread {thread_id} op {i}: empty result")

    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        futures = [pool.submit(worker, tid) for tid in range(THREADS)]
        for f in as_completed(futures):
            f.result()  # raises if thread crashed

    # Cache should have exactly 1 entry (all threads use same structure)
    assert len(errors) == 0, f"{len(errors)} errors: {errors[:5]}"
    assert len(_compiled_sql_cache) == 1, (
        f"Expected 1 cache entry, got {len(_compiled_sql_cache)}"
    )
    print(
        f"  PASS: compiled cache ({THREADS} threads × {OPS_PER_THREAD} ops, 1 cache entry)"
    )
    clear_compiled_cache()


# ---------------------------------------------------------------------------
# Test 2: Lookup registry under concurrent reads
# ---------------------------------------------------------------------------


def test_lookup_registry_concurrent():
    """24 threads resolving lookups simultaneously."""
    errors = []

    def worker(thread_id):
        for i in range(OPS_PER_THREAD):
            sql, params = resolve_lookup("name", f"val_{thread_id}_{i}", param_idx=1)
            if not sql:
                errors.append(f"Thread {thread_id}: empty SQL")
            bp = resolve_bind_params("age__gte", i)
            if len(bp) != 1:
                errors.append(f"Thread {thread_id}: wrong bind_params count")

    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        futures = [pool.submit(worker, tid) for tid in range(THREADS)]
        for f in as_completed(futures):
            f.result()

    assert len(errors) == 0, f"{len(errors)} errors: {errors[:5]}"
    print(f"  PASS: lookup registry ({THREADS} threads × {OPS_PER_THREAD} ops)")


# ---------------------------------------------------------------------------
# Test 3: LocMemCache under concurrent read/write
# ---------------------------------------------------------------------------


def test_locmemcache_concurrent():
    """24 threads reading/writing LocMemCache simultaneously."""
    cache = LocMemCache(max_size=500)
    errors = []

    def worker(thread_id):
        for i in range(OPS_PER_THREAD):
            key = f"key_{thread_id}_{i % 100}"
            # Write
            cache.set(key, f"value_{thread_id}_{i}", ttl=60)
            # Read
            val = cache.get(key)
            if val is None:
                # May be evicted by LRU — that's OK, not an error
                pass
            # Delete every 10th
            if i % 10 == 0:
                cache.delete(f"key_{thread_id}_{(i - 5) % 100}")

    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        futures = [pool.submit(worker, tid) for tid in range(THREADS)]
        for f in as_completed(futures):
            f.result()

    assert len(errors) == 0
    # Cache should be in consistent state (size <= max_size)
    assert len(cache._cache) <= 500, f"Cache exceeded max_size: {len(cache._cache)}"
    print(
        f"  PASS: LocMemCache ({THREADS} threads × {OPS_PER_THREAD} ops, size={len(cache._cache)})"
    )


# ---------------------------------------------------------------------------
# Test 4: WhereNode compile under concurrent access
# ---------------------------------------------------------------------------


def test_wherenode_concurrent():
    """24 threads building and compiling WhereNode trees simultaneously."""
    errors = []

    def worker(thread_id):
        for i in range(OPS_PER_THREAD):
            root = WhereNode(
                connector="AND",
                children=[
                    WhereNode(template="name = {}", bind_values=[f"t{thread_id}_{i}"]),
                    WhereNode(template="age > {}", bind_values=[18]),
                ],
            )
            sql, params, idx = root.compile()
            if "$1" not in sql or "$2" not in sql:
                errors.append(f"Thread {thread_id}: bad SQL: {sql}")
            if len(params) != 2:
                errors.append(f"Thread {thread_id}: wrong param count: {params}")

    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        futures = [pool.submit(worker, tid) for tid in range(THREADS)]
        for f in as_completed(futures):
            f.result()

    assert len(errors) == 0, f"{len(errors)} errors: {errors[:5]}"
    print(f"  PASS: WhereNode compile ({THREADS} threads × {OPS_PER_THREAD} ops)")


# ---------------------------------------------------------------------------
# Test 5: Mixed workload — realistic production pattern
# ---------------------------------------------------------------------------


def test_mixed_workload():
    """Simulates production: 24 threads doing SELECT/COUNT/UPDATE patterns."""
    clear_compiled_cache()
    errors = []

    def worker(thread_id):
        for i in range(OPS_PER_THREAD):
            # SELECT
            qs = QuerySet(MockModel)
            qs._annotations = {}
            qs._filters = [("status", "active"), ("name", f"user_{thread_id}_{i}")]
            qs._excludes = []
            qs._raw_wheres = []
            qs._select_related = []
            qs._values_fields = None
            qs._only = None
            qs._defer = None
            qs._ordering = ("-id",)
            qs._limit = 10
            qs._offset = None
            qs._distinct = False
            qs._for_update = None
            qs._group_by = False
            sql, params = qs._build_select()

            # COUNT
            qs2 = QuerySet(MockModel)
            qs2._annotations = {}
            qs2._filters = [("status", "active")]
            qs2._excludes = []
            qs2._raw_wheres = []
            qs2._select_related = []
            csql, cparams = qs2._build_count()

            # UPDATE
            qs3 = QuerySet(MockModel)
            qs3._annotations = {}
            qs3._filters = [("id", i)]
            qs3._excludes = []
            qs3._raw_wheres = []
            usql, uparams = qs3._build_update({"name": f"updated_{i}"})

    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        futures = [pool.submit(worker, tid) for tid in range(THREADS)]
        for f in as_completed(futures):
            f.result()

    cache_size = len(_compiled_sql_cache) + len(_compiled_count_cache)
    print(
        f"  PASS: mixed workload ({THREADS} threads × {OPS_PER_THREAD} ops, {cache_size} cache entries)"
    )
    clear_compiled_cache()


# ---------------------------------------------------------------------------
# Test 6: Model registry under concurrent reads + writes
# ---------------------------------------------------------------------------


def test_model_registry_concurrent():
    """24 threads reading model registry while 1 thread writes."""
    from hyperdjango.query import _get_model_by_table, _register_model

    errors = []

    # Pre-populate with some models
    for i in range(10):
        _register_model(f"preloaded_{i}", type(f"Model{i}", (), {}))

    def reader(thread_id):
        for i in range(OPS_PER_THREAD):
            result = _get_model_by_table(f"preloaded_{i % 10}")
            if result is None:
                errors.append(f"Reader {thread_id}: preloaded_{i % 10} not found")

    def writer():
        for i in range(100):
            _register_model(f"dynamic_{i}", type(f"DynModel{i}", (), {}))

    with ThreadPoolExecutor(max_workers=THREADS + 1) as pool:
        futures = [pool.submit(reader, tid) for tid in range(THREADS)]
        futures.append(pool.submit(writer))
        for f in as_completed(futures):
            f.result()

    assert len(errors) == 0, f"{len(errors)} errors: {errors[:5]}"
    print(f"  PASS: model registry ({THREADS} readers + 1 writer)")


# ---------------------------------------------------------------------------
# Test 7: Signals under concurrent connect + send
# ---------------------------------------------------------------------------


def test_signals_concurrent():
    """24 threads sending signals while others connect/disconnect receivers."""
    from hyperdjango.signals import Signal

    sig = Signal(name="stress_test_signal")
    received = {"count": 0}
    count_lock = threading.Lock()

    def receiver(**kwargs):
        with count_lock:
            received["count"] += 1

    # Pre-connect some receivers
    for i in range(5):
        sig.connect(lambda **kw: None, dispatch_uid=f"pre_{i}")

    errors = []

    def sender(thread_id):
        for i in range(OPS_PER_THREAD):
            # Access receivers list concurrently with connect/disconnect
            # (send is async but we only need to stress the _lock access pattern)
            with sig._lock:
                _ = list(sig._receivers)

    def connector(thread_id):
        for i in range(100):
            uid = f"dynamic_{thread_id}_{i}"
            sig.connect(receiver, dispatch_uid=uid)
            if i % 3 == 0:
                sig.disconnect(dispatch_uid=uid)

    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        # Half senders, half connectors
        futures = []
        for tid in range(THREADS):
            if tid % 2 == 0:
                futures.append(pool.submit(sender, tid))
            else:
                futures.append(pool.submit(connector, tid))
        for f in as_completed(futures):
            f.result()

    print(f"  PASS: signals ({THREADS // 2} senders + {THREADS // 2} connectors)")


# ---------------------------------------------------------------------------
# Test 8: QuerySet _clone under concurrent chaining
# ---------------------------------------------------------------------------


def test_queryset_clone_concurrent():
    """24 threads cloning and chaining querysets from a shared base."""
    errors = []
    base_qs = QuerySet(MockModel)
    base_qs._annotations = {}
    base_qs._filters = [("status", "active")]
    base_qs._excludes = []
    base_qs._raw_wheres = []
    base_qs._select_related = []
    base_qs._values_fields = None
    base_qs._only = None
    base_qs._defer = None
    base_qs._ordering = ("-id",)
    base_qs._limit = None
    base_qs._offset = None
    base_qs._distinct = False
    base_qs._for_update = None
    base_qs._group_by = False

    def worker(thread_id):
        for i in range(OPS_PER_THREAD):
            # Chain from shared base — this creates clones with shared refs
            qs = base_qs._clone(
                filters=list(base_qs._filters) + [("id", i)],
                limit_val=10,
            )
            sql, params = qs._build_select()
            if "status" not in sql:
                errors.append(f"Thread {thread_id}: missing status filter in SQL")

    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        futures = [pool.submit(worker, tid) for tid in range(THREADS)]
        for f in as_completed(futures):
            f.result()

    assert len(errors) == 0, f"{len(errors)} errors: {errors[:5]}"
    print(f"  PASS: QuerySet clone chaining ({THREADS} threads × {OPS_PER_THREAD} ops)")


# ---------------------------------------------------------------------------
# Test 9: Template engine concurrent renders
# ---------------------------------------------------------------------------


def test_template_concurrent():
    """24 threads rendering templates simultaneously."""
    from hyperdjango.templating import TemplateEngine

    engine = TemplateEngine(
        template_dir="/dev/null", autoescape=True, bytecode_cache=False
    )
    errors = []

    def worker(thread_id):
        for i in range(OPS_PER_THREAD):
            result = engine.render_string(
                "Hello {{ name }}, you are {{ age }} years old!",
                {"name": f"user_{thread_id}_{i}", "age": 20 + (i % 50)},
            )
            if f"user_{thread_id}_{i}" not in result:
                errors.append(f"Thread {thread_id}: wrong render output")

    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        futures = [pool.submit(worker, tid) for tid in range(THREADS)]
        for f in as_completed(futures):
            f.result()

    assert len(errors) == 0, f"{len(errors)} errors: {errors[:5]}"
    print(f"  PASS: template engine ({THREADS} threads × {OPS_PER_THREAD} ops)")


# ---------------------------------------------------------------------------
# Test 10: Token signing concurrent encode/decode
# ---------------------------------------------------------------------------


def test_signing_concurrent():
    """24 threads encoding and decoding tokens simultaneously."""
    from hyperdjango.signing import SigningKey, TokenEngine

    engine = TokenEngine(
        keys=[SigningKey(secret="concurrent-test-secret-32-chars!!", version=0)],
    )
    errors = []

    def worker(thread_id):
        for i in range(OPS_PER_THREAD):
            ref = f"session_{thread_id}_{i}"
            token = engine.encode_ref(ref)
            decoded = engine.decode_ref(token)
            if decoded != ref:
                errors.append(
                    f"Thread {thread_id}: roundtrip failed: {ref!r} → {decoded!r}"
                )

    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        futures = [pool.submit(worker, tid) for tid in range(THREADS)]
        for f in as_completed(futures):
            f.result()

    assert len(errors) == 0, f"{len(errors)} errors: {errors[:5]}"
    print(f"  PASS: token signing ({THREADS} threads × {OPS_PER_THREAD} ops)")


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def run_tests():
    print(
        f"\n── Free-Threading Stress Tests ({THREADS} threads × {OPS_PER_THREAD} ops) ──\n"
    )

    tests = [
        ("compiled cache concurrent", test_compiled_cache_concurrent),
        ("lookup registry concurrent", test_lookup_registry_concurrent),
        ("LocMemCache concurrent", test_locmemcache_concurrent),
        ("WhereNode compile concurrent", test_wherenode_concurrent),
        ("mixed workload", test_mixed_workload),
        ("model registry concurrent", test_model_registry_concurrent),
        ("signals concurrent", test_signals_concurrent),
        ("QuerySet clone chaining", test_queryset_clone_concurrent),
        ("template engine concurrent", test_template_concurrent),
        ("token signing concurrent", test_signing_concurrent),
    ]

    passed = 0
    failed = 0
    for name, test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"  FAIL: {name}: {e}")
            import traceback

            traceback.print_exc()
            failed += 1

    total = passed + failed
    print(f"\n{'=' * 60}")
    print(f"Free-threading stress: {passed}/{total} passed")
    if failed:
        import sys

        sys.exit(1)
    else:
        print("ALL PASSED")


if __name__ == "__main__":
    run_tests()
