#!/usr/bin/env python3
"""Test built-in profiler with nanosecond precision."""

# hyper-test: unit

import time
import traceback

from hyperdjango.profiling import (
    ProfileStore,
    RequestProfile,
    SQLQuery,
    _fmt_ns,
    elapsed_nanos,
    end_profile,
    get_current_profile,
    nanos,
    profile_handler,
    record_sql,
    start_profile,
)
from hyperdjango.testkit import check, finish, run_main

# ── Test 1: Nanosecond timestamp ──────────────────────────────────────────────


def _busy_wait_ns(reference_ns: int) -> int:
    """Wait until an INDEPENDENT monotonic clock has advanced by ``reference_ns``.

    The native profiler clock is what these tests are checking, so the interval
    they check it over must be measured by something else. Waiting for the
    reference clock to reach the mark — rather than sleeping a duration and
    assuming it did — makes the bound below exact instead of a guess about how
    close ``time.sleep`` lands to its argument on this machine.
    """
    start = time.perf_counter_ns()
    while time.perf_counter_ns() - start < reference_ns:
        time.sleep(0.0002)
    return time.perf_counter_ns() - start


def test_nanos() -> None:
    t1 = nanos()
    check("nanos() is positive", t1 > 0, f"got {t1}")
    # The reference interval is measured strictly INSIDE the two nanos() reads,
    # so `diff >= ref` holds exactly on any machine: t1 was taken before the
    # reference started and t2 after it ended. That is a far stronger claim than
    # the old "more than 500μs" floor — it says the profiler clock tracks real
    # elapsed time — and it scales with an overshooting runner instead of
    # being satisfied by any large-enough number.
    ref = _busy_wait_ns(1_000_000)  # 1ms of real time, measured not assumed
    t2 = nanos()
    check("nanos() advances", t2 > t1, f"{t2} <= {t1}")
    diff = t2 - t1
    check(
        "nanos() spans the whole reference interval",
        diff >= ref,
        f"nanos() saw {diff}ns over a {ref}ns reference interval",
    )
    print(f"  nanos() diff={_fmt_ns(diff)}")


# ── Test 2: elapsed_nanos ─────────────────────────────────────────────────────


def test_elapsed_nanos() -> None:
    start = nanos()
    # Same discipline as test_nanos: the reference interval is measured by an
    # independent clock between the two profiler reads, so the bound is exact.
    ref = _busy_wait_ns(1_000_000)
    elapsed = elapsed_nanos(start)
    check(
        "elapsed_nanos() spans the whole reference interval",
        elapsed >= ref,
        f"elapsed_nanos() saw {elapsed}ns over a {ref}ns reference interval",
    )
    print(f"  elapsed_nanos()={_fmt_ns(elapsed)}")


# ── Test 3: RequestProfile ────────────────────────────────────────────────────


def test_request_profile() -> None:
    profile = RequestProfile(
        method="GET",
        path="/users",
        total_ns=1_500_000,
        handler_ns=800_000,
        sql_total_ns=300_000,
        middleware_ns=100_000,
        routing_ns=50_000,
        sql_queries=[
            SQLQuery(sql="SELECT * FROM users", duration_ns=200_000),
            SQLQuery(sql="SELECT count(*) FROM users", duration_ns=100_000),
        ],
    )

    header = profile.to_header()
    check("X-Profile header total", "total=1.5ms" in header, header)
    check("X-Profile header handler", "handler=800.0μs" in header, header)
    check("X-Profile header sql", "sql=300.0μs(2q)" in header, header)
    print(f"  X-Profile header: {header}")

    d = profile.to_dict()
    check("to_dict sql_count", d["sql_count"] == 2, f"got {d['sql_count']}")
    check("to_dict method", d["method"] == "GET", f"got {d['method']}")
    check("to_dict queries length", len(d["queries"]) == 2, f"got {len(d['queries'])}")

    flame = profile.to_collapsed_stack()
    check(
        "collapsed stack sql frame",
        "request;handler;sql;SELECT * FROM users" in flame,
        flame,
    )
    check("collapsed stack middleware frame", "request;middleware" in flame, flame)
    print(f"  Collapsed stack format:\n{flame}")


# ── Test 4: Thread-local profiling ────────────────────────────────────────────


def test_thread_local_profiling() -> None:
    check("no ambient profile", get_current_profile() is None)

    p = start_profile(method="POST", path="/api/data")
    check("start_profile installs current", get_current_profile() is p)
    check("started profile method", p.method == "POST", f"got {p.method}")

    record_sql("INSERT INTO data VALUES ($1)", 50_000)
    record_sql("SELECT currval($1)", 10_000)
    check("record_sql counts queries", p.sql_count == 2, f"got {p.sql_count}")
    check("record_sql sums duration", p.sql_total_ns == 60_000, f"got {p.sql_total_ns}")

    completed = end_profile()
    check("end_profile returns the profile", completed is p)
    check("completed total_ns > 0", completed.total_ns > 0, f"got {completed.total_ns}")
    check("end_profile clears current", get_current_profile() is None)


# ── Test 5: profile_handler decorator ─────────────────────────────────────────


class FakeRequest:
    method = "GET"
    path = "/test"


class FakeResponse:
    def __init__(self):
        self.headers = {}


@profile_handler
def my_handler(request):
    time.sleep(0.001)  # Simulate work
    resp = FakeResponse()
    return resp


def test_profile_handler() -> None:
    resp = my_handler(FakeRequest())
    check("@profile_handler sets X-Profile", "X-Profile" in resp.headers)
    header = resp.headers["X-Profile"]
    check("@profile_handler header total", "total=" in header, header)
    check("@profile_handler header handler", "handler=" in header, header)
    print(f"  @profile_handler: {header}")


# ── Test 6: ProfileStore ──────────────────────────────────────────────────────


def test_profile_store() -> None:
    store = ProfileStore(max_profiles=5)
    for i in range(10):
        store.add(
            RequestProfile(method="GET", path=f"/p{i}", total_ns=(i + 1) * 1_000_000)
        )

    check(
        "store capped at max_profiles",
        len(store.get_all()) == 5,
        f"got {len(store.get_all())}",
    )
    slowest = store.get_slowest(3)
    check("get_slowest returns 3", len(slowest) == 3, f"got {len(slowest)}")
    check(
        "get_slowest is slowest-first",
        slowest[0].total_ns == 10_000_000,
        f"got {slowest[0].total_ns}",
    )

    flame_all = store.get_flame_graph()
    check("flame graph non-empty", len(flame_all) > 0)

    store.clear()
    check(
        "clear empties store", len(store.get_all()) == 0, f"got {len(store.get_all())}"
    )


# ── Test 7: Formatting ───────────────────────────────────────────────────────


def test_formatting() -> None:
    check("_fmt_ns nanoseconds", _fmt_ns(500) == "500ns", _fmt_ns(500))
    check("_fmt_ns microseconds", _fmt_ns(1_500) == "1.5μs", _fmt_ns(1_500))
    check("_fmt_ns milliseconds", _fmt_ns(1_500_000) == "1.5ms", _fmt_ns(1_500_000))
    check(
        "_fmt_ns seconds",
        _fmt_ns(1_500_000_000) == "1.50s",
        _fmt_ns(1_500_000_000),
    )


# ── Test 8: Performance benchmark ─────────────────────────────────────────────


def run_benchmark() -> None:
    n = 1_000_000
    start = time.perf_counter()
    for _ in range(n):
        nanos()
    elapsed = time.perf_counter() - start
    rate = n / elapsed
    overhead = elapsed / n * 1e9
    print(f"\nnanos() overhead: {overhead:.0f}ns/call ({rate:,.0f} calls/sec)")

    n = 500_000
    start_ts = nanos()
    s = time.perf_counter()
    for _ in range(n):
        elapsed_nanos(start_ts)
    elapsed = time.perf_counter() - s
    overhead = elapsed / n * 1e9
    print(f"elapsed_nanos() overhead: {overhead:.0f}ns/call")


def main() -> bool:
    for fn in (
        test_nanos,
        test_elapsed_nanos,
        test_request_profile,
        test_thread_local_profiling,
        test_profile_handler,
        test_profile_store,
        test_formatting,
    ):
        try:
            fn()
        except Exception as exc:
            # A failed check can cascade into an exception downstream; report it
            # and abort the remaining sections, as the original asserts did.
            traceback.print_exc()
            check(f"{fn.__name__} (crashed)", False, f"{type(exc).__name__}: {exc}")
            finish()
            return False
    run_benchmark()
    print()
    return finish()


if __name__ == "__main__":
    run_main(main)
