"""Free-threading (PEP 703 / 3.14t) race regressions for the settings cache,
the logging activation cache, and the Django rate-limit middleware.

Each test drives the exact concurrent access pattern that corrupted state on
a no-GIL build and asserts the corruption is gone. They also pass under the
GIL build (they just can't fail there), so they are safe in normal CI.

Covered:
  * conf.get_setting: never raises KeyError and never returns a wrong value
    while clear_settings_cache() churns the override cache.
  * logging Core.is_module_enabled: converges to the correct decision after
    concurrent enable()/disable() — no pinned stale cache entry.
  * serving.django_middleware.HyperRateLimitMiddleware: a multi-thread hammer
    on one IP admits at most `limit` requests (no lost-update bypass).
"""

import threading

import pytest

# ---------------------------------------------------------------------------
# 1. conf.get_setting under concurrent invalidation
# ---------------------------------------------------------------------------


def test_conf_get_setting_no_keyerror_or_wrong_value_under_invalidation():
    from django.conf import settings

    from hyperdjango import conf as hconf

    settings.HYPERDJANGO_POOL_SIZE = 777
    hconf.clear_settings_cache()
    assert hconf.get_setting("POOL_SIZE", 0) == 777

    stop = threading.Event()
    key_errors: list[str] = []
    wrong_values: list[object] = []
    lock = threading.Lock()

    def reader():
        while not stop.is_set():
            try:
                v = hconf.get_setting("POOL_SIZE", 0)
            except KeyError as e:  # pragma: no cover - the bug under repair
                with lock:
                    key_errors.append(repr(e))
                continue
            # The Django override is a stable 777. A correct reader only ever
            # sees the fully-populated snapshot; it must never observe the
            # DEFAULTS/env fallback while the override exists.
            if v != 777:
                with lock:
                    if len(wrong_values) < 10:
                        wrong_values.append(v)

    def invalidator():
        for _ in range(20000):
            hconf.clear_settings_cache()
        stop.set()

    readers = [threading.Thread(target=reader) for _ in range(10)]
    inv = threading.Thread(target=invalidator)
    for t in readers:
        t.start()
    inv.start()
    inv.join()
    stop.set()
    for t in readers:
        t.join()

    try:
        assert key_errors == [], f"get_setting raised KeyError: {key_errors[:3]}"
        assert wrong_values == [], (
            f"get_setting returned wrong values (override lost): {wrong_values}"
        )
    finally:
        if hasattr(settings, "HYPERDJANGO_POOL_SIZE"):
            del settings.HYPERDJANGO_POOL_SIZE
        hconf.clear_settings_cache()


# ---------------------------------------------------------------------------
# 2. logging Core.is_module_enabled convergence
# ---------------------------------------------------------------------------


def _set_activation(core, prefix, enabled):
    with core.lock:
        core.activation_list = [(n, s) for n, s in core.activation_list if n != prefix]
        core.activation_list.insert(0, (prefix, enabled))
        core.activation_list.sort(key=lambda x: -len(x[0]))
        core.enabled_cache.clear()


def _fresh_decision(core, name):
    status = True
    dotted = name + "."
    for prefix, enabled in list(core.activation_list):
        if dotted.startswith(prefix):
            status = enabled
            break
    return status


def test_logging_is_module_enabled_converges_no_stuck_stale():
    import time

    from hyperdjango.logging._core import Core

    name = "mod.x.sub"
    prefix = "mod.x."
    rounds = 20
    flips = 8000
    stuck = 0

    for r in range(rounds):
        core = Core()
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                core.is_module_enabled(name)

        readers = [threading.Thread(target=reader) for _ in range(8)]
        for t in readers:
            t.start()

        for i in range(flips):
            _set_activation(core, prefix, i % 2 == 0)
        _set_activation(core, prefix, r % 2 == 0)  # deterministic final state

        time.sleep(0.002)  # window for a reader to pin a stale entry
        stop.set()
        for t in readers:
            t.join()

        if core.is_module_enabled(name) != _fresh_decision(core, name):
            stuck += 1

    assert stuck == 0, (
        f"is_module_enabled pinned a stale decision in {stuck}/{rounds} rounds"
    )


# ---------------------------------------------------------------------------
# 3. HyperRateLimitMiddleware — no lost-update bypass
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self):
        self.status_code = 200
        self.headers: dict[str, str] = {}

    def __setitem__(self, key, value):
        self.headers[key] = value


class _FakeRequest:
    def __init__(self, ip="10.0.0.1"):
        self.META = {"REMOTE_ADDR": ip}


@pytest.mark.parametrize("limit", [100])
def test_ratelimit_middleware_not_bypassable_under_threads(limit):
    from django.conf import settings

    from hyperdjango.serving.django_middleware import HyperRateLimitMiddleware

    old_limit = getattr(settings, "HYPERDJANGO_RATE_LIMIT_REQUESTS", None)
    old_window = getattr(settings, "HYPERDJANGO_RATE_LIMIT_WINDOW", None)
    settings.HYPERDJANGO_RATE_LIMIT_REQUESTS = limit
    settings.HYPERDJANGO_RATE_LIMIT_WINDOW = 3600

    served = 0
    served_lock = threading.Lock()

    def get_response(_request):
        nonlocal served
        with served_lock:
            served += 1
        return _FakeResponse()

    try:
        mw = HyperRateLimitMiddleware(get_response)

        n_threads, iters = 16, 2000
        barrier = threading.Barrier(n_threads)
        allowed = 0
        allowed_lock = threading.Lock()

        def worker():
            nonlocal allowed
            barrier.wait()
            for _ in range(iters):
                resp = mw(_FakeRequest())
                if getattr(resp, "status_code", 200) != 429:
                    with allowed_lock:
                        allowed += 1

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Every non-429 response is a real service call, and the limiter must
        # cap them at `limit` — no lost-update undercount lets extras through.
        assert allowed <= limit, f"limiter bypassed: {allowed} admitted > limit {limit}"
        assert served == allowed, "served count must equal non-429 count"

        # And every admitted request was actually counted in the window (no
        # lost-update where a racing increment clobbers another's). The backend
        # bucket is [tokens, last_refill, window_idx, admitted_in_window]; with a
        # single key and a 1h window, admitted_in_window must equal what we served.
        buckets, _ = mw._backend._shard_for("10.0.0.1")
        admitted = int(buckets["10.0.0.1"][3])
        assert admitted == served, (
            f"lost updates: admitted {admitted} != served {served}"
        )
    finally:
        if old_limit is None:
            if hasattr(settings, "HYPERDJANGO_RATE_LIMIT_REQUESTS"):
                del settings.HYPERDJANGO_RATE_LIMIT_REQUESTS
        else:
            settings.HYPERDJANGO_RATE_LIMIT_REQUESTS = old_limit
        if old_window is None:
            if hasattr(settings, "HYPERDJANGO_RATE_LIMIT_WINDOW"):
                del settings.HYPERDJANGO_RATE_LIMIT_WINDOW
        else:
            settings.HYPERDJANGO_RATE_LIMIT_WINDOW = old_window
