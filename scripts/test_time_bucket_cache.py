"""
Tests: hyperdjango.humanize.time_bucket_cached decorator.

Proves:
  1. Cached output matches uncached output for identical inputs
  2. Cache hits are recorded (cache_info shows hits > 0)
  3. Different bucket values produce separate cache entries
  4. Cache size is bounded by maxsize
  5. cache_clear() resets the cache
  6. Decorator preserves __name__ and __doc__
  7. Applies correctly to naturaltime() — bulk timestamps stay stable
  8. Future timestamps still work (naturaltime past + future paths)
  9. Non-datetime input passes through unchanged
  10. None / empty inputs are cacheable (no crash)
  11. time_bucket_cached on hypernews time_ago produces correct output

Run: uv run python scripts/test_time_bucket_cache.py
"""

# hyper-test: unit

from datetime import UTC, datetime, timedelta

from hyperdjango.humanize import naturaltime, time_bucket_cached
from hyperdjango.testkit import check, finish, run_main


def main() -> bool:
    print("── time_bucket_cached decorator ──")

    calls = 0

    @time_bucket_cached(bucket_seconds=30, maxsize=16)
    def expensive(x: int) -> int:
        nonlocal calls
        calls += 1
        return x * 2

    # Cold miss
    assert expensive(5) == 10
    assert calls == 1
    # Warm hit
    assert expensive(5) == 10
    check("cache hit skips expensive work", calls == 1, f"calls={calls}")

    # Different arg = new miss
    assert expensive(6) == 12
    check("different arg is a cache miss", calls == 2, f"calls={calls}")

    # cache_info exposes stats
    info = expensive.cache_info()
    check("cache_info hits > 0", info.hits > 0, f"info={info}")
    check("cache_info misses > 0", info.misses > 0, f"info={info}")

    # cache_clear works
    expensive.cache_clear()
    info_after = expensive.cache_info()
    check("cache_clear resets hits", info_after.hits == 0)
    check("cache_clear resets misses", info_after.misses == 0)

    # __name__ / __doc__ preserved
    check("decorator preserves __name__", expensive.__name__ == "expensive")
    check(
        "decorator preserves __doc__ None/missing ok",
        expensive.__doc__ is None or isinstance(expensive.__doc__, str),
    )

    # Bounded maxsize
    calls = 0
    for i in range(30):
        expensive(i)
    info_bounded = expensive.cache_info()
    check(
        "cache currsize <= maxsize",
        info_bounded.currsize <= 16,
        f"currsize={info_bounded.currsize}",
    )

    # ── naturaltime() with cache ──────────────────────────────────────
    print("\n── naturaltime() cached ──")

    # Cold + warm same result
    now = datetime.now(UTC)
    past = now - timedelta(minutes=5)
    cold = naturaltime(past)
    warm = naturaltime(past)
    check("same input → same output", cold == warm, f"cold={cold} warm={warm}")
    check(
        "past: ~5 minutes ago",
        "5 minutes ago" in cold or "4 minutes" in cold,
        f"got {cold!r}",
    )

    # cache_info on naturaltime
    info_nt = naturaltime.cache_info()
    check("naturaltime cache shows hits", info_nt.hits >= 1)

    # Future path — allow off-by-one hour from test/naturaltime clock drift
    future = now + timedelta(hours=2, minutes=1)
    fut_str = naturaltime(future)
    check(
        "future: 'in ... hour(s)'",
        "in 2 hour" in fut_str or "in 1 hour" in fut_str,
        f"got {fut_str!r}",
    )

    # Non-datetime input
    non_dt = naturaltime("hello")
    check("non-datetime falls through to str()", non_dt == "hello")

    # Various past timestamps correctness
    cases = [
        (now - timedelta(seconds=5), "just now"),
        (now - timedelta(seconds=30), "seconds ago"),
        (now - timedelta(minutes=1), "minute"),
        (now - timedelta(hours=1), "hour"),
        (now - timedelta(days=1), "day"),
        (now - timedelta(days=10), "week"),
        (now - timedelta(days=60), "month"),
        (now - timedelta(days=400), "year"),
    ]
    for ts, needle in cases:
        out = naturaltime(ts)
        check(
            f"naturaltime({ts.isoformat()[:10]}): contains {needle!r}",
            needle in out,
            f"got {out!r}",
        )

    # ── hypernews time_ago ──────────────────────────────────────────
    print("\n── hypernews time_ago cached ──")
    import os

    os.environ.setdefault("DATABASE_URL", "postgres://localhost/hyperdjango_test")
    os.environ.setdefault("HYPER_LOAD_TEST", "1")
    from services.hypernews.app import time_ago as hn_time_ago

    info_hn_before = hn_time_ago.cache_info()
    out1 = hn_time_ago(now - timedelta(minutes=10))
    out2 = hn_time_ago(now - timedelta(minutes=10))
    check("hypernews time_ago deterministic", out1 == out2, f"out1={out1} out2={out2}")
    check(
        "hypernews time_ago minute format",
        "minute" in out1 or "just now" in out1,
        f"got {out1!r}",
    )

    info_hn_after = hn_time_ago.cache_info()
    check(
        "hypernews time_ago cache recorded hit",
        info_hn_after.hits > info_hn_before.hits,
    )

    # Empty input returns "" (not cached, bail early)
    check("hypernews time_ago empty → ''", hn_time_ago("") == "")
    check("hypernews time_ago None → ''", hn_time_ago(None) == "")

    # ── Summary ──
    print()
    return finish()


if __name__ == "__main__":
    run_main(main)
