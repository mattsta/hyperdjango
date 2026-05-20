"""Free-threading (no-GIL) race regression tests for singletons / perf /
templating / tasks.

These exercise the fixes in ws21-singleton-py. They pass under both GIL and
free-threaded builds, but only actually PROVE the fix under free-threaded
CPython 3.14t (run with PYTHON_GIL=0 and a 3.14t interpreter), where the racing
threads truly run in parallel. Each test synchronizes N threads on a barrier so
they hit the racing first-call simultaneously.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import threading
from pathlib import Path

import pytest


def _gil_note() -> str:
    is_ft = hasattr(sys, "_is_gil_enabled")
    enabled = sys._is_gil_enabled() if is_ft else True
    return f"free-threaded-build={is_ft} gil_enabled={enabled}"


def _race(fn, n: int = 64):
    """Run fn() from n threads released simultaneously; return list of results."""
    barrier = threading.Barrier(n)
    results: list = [None] * n
    errors: list = []

    def worker(i: int):
        try:
            barrier.wait()
            results[i] = fn()
        except Exception as e:  # pragma: no cover - surfaced via assert
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"racing workers raised: {errors!r}"
    return results


class _Counter:
    def __init__(self):
        self._lock = threading.Lock()
        self.n = 0

    def bump(self):
        with self._lock:
            self.n += 1


# ── 1. Lazy-init double-init: double-checked-locking singletons ─────────────


def test_ws_recv_executor_single_instance_under_race():
    import hyperdjango.websocket as ws

    counter = _Counter()
    orig = ws.ThreadPoolExecutor

    def counting(*a, **k):
        counter.bump()
        return orig(*a, **k)

    ws.ThreadPoolExecutor = counting
    try:
        ws._ws_recv_executor_instance = None
        results = _race(ws._ws_recv_executor, n=64)
    finally:
        ws.ThreadPoolExecutor = orig

    inst = results[0]
    assert inst is not None
    assert all(r is inst for r in results), "executor not shared across threads"
    assert counter.n == 1, f"{counter.n} executors built ({_gil_note()})"

    inst.shutdown(wait=False)
    ws._ws_recv_executor_instance = None


def test_query_cache_manager_single_instance_under_race():
    import hyperdjango.query_cache as qc

    counter = _Counter()
    orig = qc.QueryCacheManager

    class Counting(orig):
        def __init__(self, *a, **k):
            counter.bump()
            super().__init__(*a, **k)

    qc.QueryCacheManager = Counting
    try:
        qc._query_cache_manager = None
        results = _race(qc.get_query_cache, n=64)
    finally:
        qc.QueryCacheManager = orig
        qc._query_cache_manager = None

    inst = results[0]
    assert all(r is inst for r in results), "query cache not shared"
    assert counter.n == 1, f"{counter.n} managers built ({_gil_note()})"


def test_connections_manager_single_instance_under_race():
    import hyperdjango.multi_db as mdb

    counter = _Counter()
    orig = mdb.ConnectionManager

    class Counting(orig):
        def __init__(self, *a, **k):
            counter.bump()
            super().__init__(*a, **k)

    mdb.ConnectionManager = Counting
    try:
        mdb._connections = None
        results = _race(mdb.get_connections, n=64)
    finally:
        mdb.ConnectionManager = orig
        mdb._connections = None

    inst = results[0]
    assert all(r is inst for r in results), "connection manager not shared"
    assert counter.n == 1, f"{counter.n} managers built ({_gil_note()})"


def test_app_db_single_database_under_race():
    import hyperdjango.database as dbmod
    from hyperdjango.app import HyperApp

    counter = _Counter()
    orig = dbmod.Database

    class FakeDB:
        def __init__(self, url):
            counter.bump()
            self.url = url

    dbmod.Database = FakeDB
    try:
        app = HyperApp(database="postgres://localhost/racedb")
        results = _race(lambda: app.db, n=64)
    finally:
        dbmod.Database = orig

    inst = results[0]
    assert isinstance(inst, FakeDB)
    assert all(r is inst for r in results), "app.db not shared"
    assert counter.n == 1, f"{counter.n} Database objects (pools) built ({_gil_note()})"


def test_app_render_publishes_fully_configured_engine_under_race():
    """A concurrent renderer must never see a half-configured template engine:
    the static/app_version globals must be registered BEFORE the engine is
    published, or {{ static(...) }} silently renders empty."""
    from hyperdjango.app import HyperApp

    tdir = tempfile.mkdtemp()
    Path(tdir, "page.html").write_text(
        "URL={{ static('x.css') }}|VER={{ app_version }}"
    )
    app = HyperApp(templates=tdir)

    def render_once():
        resp = app.render("page.html", {})
        return resp.body.decode("utf-8")

    results = _race(render_once, n=64)

    # Every racing render must have seen the fully-configured engine, so every
    # output has the resolved static URL (not an empty string from a missing
    # global on a half-published engine).
    for out in results:
        assert out.startswith("URL=/static/x.css|VER="), (
            f"half-configured engine observed: {out!r} ({_gil_note()})"
        )
    eng = app._template_engine
    assert eng is not None
    assert "static" in eng._globals and "app_version" in eng._globals


# ── 2. tasks.py TaskQueue.start(): exactly one worker set ───────────────────


def test_task_queue_start_spawns_exactly_one_worker_set():
    from hyperdjango.tasks import TaskDecorator, TaskQueue

    num_workers = 4
    tq = TaskQueue(workers=num_workers)

    def noop():
        return 1

    deco = TaskDecorator(noop, task_queue=tq)

    # Race many concurrent FIRST .delay() calls — each does an unlocked
    # `if not running: start()` check, so without the in-start lock several
    # callers would each spawn a full worker set.
    try:
        _race(lambda: deco.delay(), n=32)
        assert tq._running is True
        assert len(tq._workers) == num_workers, (
            f"{len(tq._workers)} workers spawned, expected {num_workers} "
            f"({_gil_note()})"
        )
        live = [t for t in threading.enumerate() if t.name.startswith("task-worker-")]
        assert len(live) == num_workers, (
            f"{len(live)} live task-worker threads, expected {num_workers}"
        )
    finally:
        tq.stop()

    assert tq._running is False
    assert len(tq._workers) == 0


# ── 3. performance.py: contextvar isolation + no lost counter increments ────


class _FakeRequest:
    def __init__(self, path):
        self.path = path
        self.method = "GET"


class _FakeResponse:
    def __init__(self):
        self.headers: dict[str, str] = {}


@pytest.mark.asyncio
async def test_perf_queries_not_cross_attributed_across_await():
    """Two requests on the SAME event-loop thread, interleaved across an await,
    must not cross-attribute their queries (contextvar, not threading.local)."""
    from hyperdjango.performance import PerformanceMiddleware

    perf = PerformanceMiddleware(slow_query_threshold_ms=10_000)

    a_recorded = asyncio.Event()
    b_done = asyncio.Event()

    async def call_next_a(request):
        perf.record_query("SELECT A1", 1.0)
        perf.record_query("SELECT A2", 1.0)
        a_recorded.set()  # A has 2 queries, now suspend
        await b_done.wait()  # B runs on this same thread while A is suspended
        perf.record_query("SELECT A3", 1.0)  # still attributed to A after resume
        return _FakeResponse()

    async def call_next_b(request):
        await a_recorded.wait()  # start only once A is suspended
        perf.record_query("SELECT B1", 1.0)
        b_done.set()
        return _FakeResponse()

    resp_a, resp_b = await asyncio.gather(
        perf(_FakeRequest("/a"), call_next_a),
        perf(_FakeRequest("/b"), call_next_b),
    )

    assert resp_a.headers["X-Query-Count"] == "3", (
        f"A cross-attributed: {resp_a.headers} ({_gil_note()})"
    )
    assert resp_b.headers["X-Query-Count"] == "1", (
        f"B cross-attributed: {resp_b.headers} ({_gil_note()})"
    )


def test_perf_counters_no_lost_increments_under_race():
    """Fast-path counter updates must be atomic (under the lock) so concurrent
    requests don't lose _total_queries / _slow_count increments."""
    import hyperdjango.performance as perfmod
    from hyperdjango.performance import PerformanceMiddleware

    perf = PerformanceMiddleware(slow_query_threshold_ms=0.0)  # every query is "slow"

    n_threads = 8
    per_thread = 5000
    barrier = threading.Barrier(n_threads)

    def worker():
        # Each thread gets its own contextvar context: open a request window.
        perfmod._perf_queries.set([])
        perfmod._perf_in_request.set(True)
        barrier.wait()
        for _ in range(per_thread):
            perf.record_query("SELECT 1", 5.0)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    expected = n_threads * per_thread
    assert perf._total_queries == expected, (
        f"lost increments: {perf._total_queries} != {expected} ({_gil_note()})"
    )
    assert perf._slow_count == expected, (
        f"lost slow increments: {perf._slow_count} != {expected} ({_gil_note()})"
    )


# ── 4. templating.py: complete dependency snapshot under concurrent compiles ─


def test_concurrent_compiles_produce_complete_dependency_snapshot():
    """Concurrent compiles on ONE engine must each write a COMPLETE Merkle
    dependency snapshot to .hztc.meta — every child extends base.html, so every
    child's meta must record base.html as a dependency. A shared _loaded_sources
    dict would let one compile's clear() drop another's dependency."""
    from hyperdjango.templating import TemplateEngine

    tdir = tempfile.mkdtemp()
    bdir = tempfile.mkdtemp()
    Path(tdir, "base.html").write_text("{% block c %}base{% endblock %}")

    n_children = 40
    for i in range(n_children):
        Path(tdir, f"child_{i}.html").write_text(
            '{% extends "base.html" %}{% block c %}child ' + str(i) + "{% endblock %}"
        )

    eng = TemplateEngine(template_dir=tdir, bytecode_cache_dir=bdir, auto_reload=False)

    barrier = threading.Barrier(n_children)
    errors: list = []

    def compile_child(i: int):
        try:
            barrier.wait()
            eng.render(f"child_{i}.html", {})
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [
        threading.Thread(target=compile_child, args=(i,)) for i in range(n_children)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"compile raised: {errors!r}"

    # Every child's on-disk dependency snapshot must include base.html.
    missing = []
    for i in range(n_children):
        mp = eng._meta_path(f"child_{i}.html")
        assert mp.exists(), f"no meta for child_{i}"
        meta = json.loads(mp.read_text())
        if "base.html" not in meta.get("dep_hashes", {}):
            missing.append(i)

    assert not missing, (
        f"{len(missing)} children have an incomplete dependency snapshot "
        f"(missing base.html): {missing[:10]} ({_gil_note()})"
    )
