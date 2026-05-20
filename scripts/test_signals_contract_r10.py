"""Signal dispatch contract (task R10).

Verifies the fail-fast / robust split in ``hyperdjango.signals`` and the
data-correctness guarantee of the query-cache invalidation receivers:

  * ``Signal.send`` PROPAGATES the first receiver exception and STOPS
    iterating (a pre-commit / validating receiver can veto the operation).
  * ``Signal.send`` returns ``[(receiver, return_value), ...]`` on success.
  * ``Signal.send_robust`` CATCHES every receiver exception, calls every
    receiver, and NEVER raises — a failing receiver is captured as its
    ``(receiver, exception)`` response.
  * ``log_robust_responses`` surfaces those captured failures LOUDLY at
    error level (with traceback) — never silent.
  * A raising ``post_save`` cache-invalidation receiver is LOGGED (not
    silent) and does NOT leave a definitively-stale positive cache entry:
    it degrades to a global invalidate so reads miss rather than serve stale.

Run:  uv run hyper-test signals_contract_r10
"""

# hyper-test: unit

import asyncio
import logging

from hyperdjango.query_cache import (
    QueryCacheManager,
    get_query_cache,
    set_query_cache,
)
from hyperdjango.signals import Signal, log_robust_responses, post_save

_PASS = 0
_FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if condition:
        _PASS += 1
        print(f"  PASS  {name}")
    else:
        _FAIL += 1
        print(f"  FAIL  {name}  {detail}")


class _Boom(RuntimeError):
    """Distinctive receiver failure."""


class _CapturingHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


# ── send(): fail-fast propagation ───────────────────────────────────────────


def test_send_propagates_first_exception() -> None:
    sig = Signal(name="t")
    ran_second = {"v": False}

    def bad(sender, **kw):
        raise _Boom("first receiver failed")

    def second(sender, **kw):
        ran_second["v"] = True
        return "ok"

    sig.connect(bad)
    sig.connect(second)

    raised = None
    try:
        asyncio.run(sig.send(sender=None))
    except _Boom as e:
        raised = e

    check("send() propagates the raising receiver", isinstance(raised, _Boom))
    check(
        "send() stops iterating after the failure (fail-fast)",
        ran_second["v"] is False,
        "second receiver ran despite earlier failure",
    )


def test_send_returns_values_on_success() -> None:
    sig = Signal(name="t")

    async def a(sender, **kw):
        return "a-val"

    def b(sender, **kw):
        return "b-val"

    sig.connect(a)
    sig.connect(b)

    responses = asyncio.run(sig.send(sender=None))
    values = [r for _, r in responses]
    check(
        "send() returns (receiver, return_value) for every receiver on success",
        values == ["a-val", "b-val"],
        f"got {values!r}",
    )


# ── send_robust(): collect, never raise ─────────────────────────────────────


def test_send_robust_collects_without_raising() -> None:
    sig = Signal(name="t")
    ran_second = {"v": False}

    def bad(sender, **kw):
        raise _Boom("robust receiver failed")

    def second(sender, **kw):
        ran_second["v"] = True
        return "ok"

    sig.connect(bad)
    sig.connect(second)

    raised = None
    responses = None
    try:
        responses = asyncio.run(sig.send_robust(sender=None))
    except BaseException as e:  # noqa: BLE001 - test asserts it must NOT raise
        raised = e

    check("send_robust() does not raise", raised is None, repr(raised))
    check(
        "send_robust() still calls later receivers after a failure",
        ran_second["v"] is True,
    )
    check(
        "send_robust() captures the exception as the receiver's response",
        responses is not None
        and isinstance(responses[0][1], _Boom)
        and responses[1][1] == "ok",
        f"got {responses!r}",
    )


def test_log_robust_responses_is_loud() -> None:
    sig = Signal(name="notify")

    def bad(sender, **kw):
        raise _Boom("captured")

    sig.connect(bad)
    responses = asyncio.run(sig.send_robust(sender=None))

    logger = logging.getLogger("hyperdjango.test.robust")
    logger.setLevel(logging.ERROR)
    handler = _CapturingHandler()
    logger.addHandler(handler)
    try:
        n = log_robust_responses(responses, logger, "notify")
    finally:
        logger.removeHandler(handler)

    check("log_robust_responses reports the failure count", n == 1, f"n={n}")
    check(
        "log_robust_responses emits an ERROR record (loud, not silent)",
        len(handler.records) == 1 and handler.records[0].levelno == logging.ERROR,
    )
    check(
        "logged record carries the traceback (exc_info)",
        bool(handler.records) and handler.records[0].exc_info is not None,
    )


# ── post_save cache invalidation: loud + safe on receiver failure ────────────


class _FakeMeta:
    table = "widgets"


class _FakeInstance:
    _meta = _FakeMeta()
    pk = 1


def test_post_save_invalidation_loud_and_safe_on_failure() -> None:
    # Fresh manager so we don't disturb the app-wide cache.
    prev = get_query_cache()
    mgr = QueryCacheManager(default_ttl=60)
    set_query_cache(mgr)

    # Seed a positive cache entry for the table (pre-write cached rows).
    key = mgr.make_key("widgets", "SELECT * FROM widgets WHERE id=$1", (1,))
    mgr.set(key, [{"id": 1, "name": "stale"}])
    seeded_ok = mgr.get(key) is not None

    # Force the targeted invalidation path to fail (transient backend error).
    def _boom_invalidate(table, pk):
        raise _Boom("backend invalidation exploded")

    mgr.invalidate_row = _boom_invalidate  # type: ignore[method-assign]

    qc_logger = logging.getLogger("hyperdjango.query_cache")
    handler = _CapturingHandler()
    prev_level = qc_logger.level
    qc_logger.setLevel(logging.ERROR)
    qc_logger.addHandler(handler)

    raised = None
    try:
        # Real post_save dispatch, robust: receiver failure must be captured,
        # not propagated. This exercises _on_post_save -> _safe_invalidate.
        asyncio.run(
            post_save.send_robust(sender=_FakeInstance, instance=_FakeInstance())
        )
    except BaseException as e:  # noqa: BLE001
        raised = e
    finally:
        qc_logger.removeHandler(handler)
        qc_logger.setLevel(prev_level)
        set_query_cache(prev)

    check("seed: positive cache entry was present before the write", seeded_ok)
    check(
        "post_save.send_robust does not propagate the receiver failure",
        raised is None,
        repr(raised),
    )
    logged_error = any(r.levelno >= logging.ERROR for r in handler.records)
    check(
        "failed invalidation is LOGGED loudly (not silent)",
        logged_error,
        f"records={handler.records!r}",
    )
    # Fail-safe: the fallback invalidate_all cleared the backend AND bumped the
    # generation, so the old positive entry can no longer be served.
    check(
        "stale positive entry is gone after the failed invalidation",
        mgr.get(key) is None,
    )
    check(
        "generation/version advanced so recomputed key differs (reads miss)",
        mgr.make_key("widgets", "SELECT * FROM widgets WHERE id=$1", (1,)) != key,
    )


def run() -> bool:
    test_send_propagates_first_exception()
    test_send_returns_values_on_success()
    test_send_robust_collects_without_raising()
    test_log_robust_responses_is_loud()
    test_post_save_invalidation_loud_and_safe_on_failure()
    print(f"\n{'=' * 60}")
    print(f"Results: {_PASS} passed, {_FAIL} failed")
    print(f"{'=' * 60}")
    return _FAIL == 0


if __name__ == "__main__":
    import sys

    sys.exit(0 if run() else 1)
