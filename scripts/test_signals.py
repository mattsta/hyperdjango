#!/usr/bin/env python3
"""
Tests for the signal/event system.

Usage:
    uv run hyper-test signals
"""

# hyper-test: unit

import asyncio
import sys

from hyperdjango.signals import (
    Signal,
    post_delete,
    post_save,
    pre_delete,
    pre_save,
    request_finished,
    request_started,
    user_logged_in,
    user_logged_out,
    user_login_failed,
)

RESULTS = {"passed": 0, "failed": 0, "errors": []}


def check(name, condition, details=""):
    if condition:
        RESULTS["passed"] += 1
        print(f"  PASS: {name}")
    else:
        RESULTS["failed"] += 1
        RESULTS["errors"].append(name)
        print(f"  FAIL: {name} — {details}")


def main():
    print("=" * 60)
    print("Signal/Event System Tests")
    print("=" * 60)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    print("\n--- Signal Basic ---")
    loop.run_until_complete(test_signal_basic())

    print("\n--- Signal Decorator ---")
    loop.run_until_complete(test_signal_decorator())

    print("\n--- Signal Sync Receiver ---")
    loop.run_until_complete(test_signal_sync_receiver())

    print("\n--- Signal Multiple Receivers ---")
    loop.run_until_complete(test_signal_multiple_receivers())

    print("\n--- Signal Disconnect ---")
    loop.run_until_complete(test_signal_disconnect())

    print("\n--- Signal Duplicate Prevention ---")
    loop.run_until_complete(test_signal_duplicate())

    print("\n--- Signal dispatch_uid ---")
    loop.run_until_complete(test_signal_dispatch_uid())

    print("\n--- Signal Error Handling ---")
    loop.run_until_complete(test_signal_error_handling())

    print("\n--- Signal has_receivers ---")
    loop.run_until_complete(test_signal_has_receivers())

    print("\n--- Signal kwargs ---")
    loop.run_until_complete(test_signal_kwargs())

    print("\n--- Signal sender ---")
    loop.run_until_complete(test_signal_sender())

    print("\n--- Built-in Signals ---")
    test_builtin_signals()

    print("\n--- Signal repr ---")
    test_signal_repr()

    print("\n--- Signal Thread Safety ---")
    test_signal_thread_safety()

    total = RESULTS["passed"] + RESULTS["failed"]
    print(f"\n{'=' * 60}")
    print(f"Results: {RESULTS['passed']}/{total} passed, {RESULTS['failed']} failed")
    if RESULTS["errors"]:
        print("Failed:")
        for e in RESULTS["errors"]:
            print(f"  - {e}")
    print(f"{'=' * 60}")
    return 0 if RESULTS["failed"] == 0 else 1


async def test_signal_basic():
    sig = Signal(name="test")
    results = []

    async def handler(sender, **kwargs):
        results.append(("handled", kwargs.get("value")))

    sig.connect(handler)
    await sig.send(sender=None, value=42)

    check("basic send calls handler", len(results) == 1)
    check("basic send passes kwargs", results[0] == ("handled", 42))


async def test_signal_decorator():
    sig = Signal(name="decorated")
    results = []

    @sig.connect
    async def handler(sender, **kwargs):
        results.append(kwargs.get("msg"))

    await sig.send(sender=None, msg="hello")
    check("decorator connects handler", len(results) == 1)
    check("decorator handler receives kwargs", results[0] == "hello")


async def test_signal_sync_receiver():
    sig = Signal(name="sync")
    results = []

    def sync_handler(sender, **kwargs):
        results.append("sync_called")
        return "sync_result"

    sig.connect(sync_handler)
    responses = await sig.send(sender=None)

    check("sync receiver called", len(results) == 1)
    check("sync receiver returns value", responses[0][1] == "sync_result")


async def test_signal_multiple_receivers():
    sig = Signal(name="multi")
    order = []

    async def handler1(sender, **kwargs):
        order.append(1)

    async def handler2(sender, **kwargs):
        order.append(2)

    async def handler3(sender, **kwargs):
        order.append(3)

    sig.connect(handler1)
    sig.connect(handler2)
    sig.connect(handler3)

    await sig.send(sender=None)
    check("all 3 receivers called", len(order) == 3)
    check("receivers called in connect order", order == [1, 2, 3])
    check("receiver_count is 3", sig.receiver_count == 3)


async def test_signal_disconnect():
    sig = Signal(name="disconnect")
    results = []

    async def handler(sender, **kwargs):
        results.append("called")

    sig.connect(handler)
    await sig.send(sender=None)
    check("handler called before disconnect", len(results) == 1)

    removed = sig.disconnect(handler)
    check("disconnect returns True", removed is True)

    await sig.send(sender=None)
    check("handler not called after disconnect", len(results) == 1)

    removed2 = sig.disconnect(handler)
    check("disconnect returns False for unknown", removed2 is False)


async def test_signal_duplicate():
    sig = Signal(name="dedup")
    results = []

    async def handler(sender, **kwargs):
        results.append("called")

    sig.connect(handler)
    sig.connect(handler)  # Duplicate — should be ignored
    sig.connect(handler)  # Duplicate — should be ignored

    await sig.send(sender=None)
    check("duplicate prevention — called once", len(results) == 1)
    check("receiver_count is 1", sig.receiver_count == 1)


async def test_signal_dispatch_uid():
    sig = Signal(name="uid")
    results = []

    async def handler1(sender, **kwargs):
        results.append("h1")

    async def handler2(sender, **kwargs):
        results.append("h2")

    sig.connect(handler1, dispatch_uid="unique_handler")
    sig.connect(handler2, dispatch_uid="unique_handler")  # Should be ignored (same uid)

    await sig.send(sender=None)
    check("dispatch_uid prevents duplicate", len(results) == 1)
    check("first handler wins", results[0] == "h1")

    # Disconnect by uid
    sig.disconnect(dispatch_uid="unique_handler")
    results.clear()
    await sig.send(sender=None)
    check("disconnect by uid works", len(results) == 0)


async def test_signal_error_handling():
    sig = Signal(name="errors")
    results = []

    async def good_handler(sender, **kwargs):
        results.append("good")
        return "ok"

    async def bad_handler(sender, **kwargs):
        raise ValueError("intentional error")

    async def another_good(sender, **kwargs):
        results.append("also good")
        return "also ok"

    sig.connect(good_handler)
    sig.connect(bad_handler)
    sig.connect(another_good)

    # send_robust() is the collect-and-continue variant (send() now fail-fast
    # propagates the first receiver exception — the round-10 contract fix).
    responses = await sig.send_robust(sender=None)
    check("all receivers attempted", len(responses) == 3)
    check("good handler succeeded", responses[0][1] == "ok")
    check("bad handler returned exception", isinstance(responses[1][1], ValueError))
    check("third handler still called", responses[2][1] == "also ok")
    check("both good handlers ran", len(results) == 2)


async def test_signal_has_receivers():
    sig = Signal(name="check")
    check("no receivers initially", not sig.has_receivers())

    async def handler(sender, **kwargs):
        pass

    sig.connect(handler)
    check("has receivers after connect", sig.has_receivers())

    sig.disconnect(handler)
    check("no receivers after disconnect", not sig.has_receivers())


async def test_signal_kwargs():
    sig = Signal(name="kwargs")
    received = {}

    async def handler(sender, **kwargs):
        received.update(kwargs)

    sig.connect(handler)
    await sig.send(sender=None, user_id=1, action="create", model="Product")

    check("kwargs user_id", received.get("user_id") == 1)
    check("kwargs action", received.get("action") == "create")
    check("kwargs model", received.get("model") == "Product")


async def test_signal_sender():
    sig = Signal(name="sender")
    received_sender = [None]

    async def handler(sender, **kwargs):
        received_sender[0] = sender

    sig.connect(handler)

    class MyView:
        pass

    await sig.send(sender=MyView)
    check("sender is class", received_sender[0] is MyView)

    await sig.send(sender="string_sender")
    check("sender is string", received_sender[0] == "string_sender")


def test_builtin_signals():
    """Verify all built-in signals exist and are Signal instances."""
    signals = [
        ("pre_save", pre_save),
        ("post_save", post_save),
        ("pre_delete", pre_delete),
        ("post_delete", post_delete),
        ("user_logged_in", user_logged_in),
        ("user_logged_out", user_logged_out),
        ("user_login_failed", user_login_failed),
        ("request_started", request_started),
        ("request_finished", request_finished),
    ]
    for name, sig in signals:
        check(f"built-in {name} exists", isinstance(sig, Signal))
        check(f"built-in {name} has name", sig.name == name)


def test_signal_repr():
    sig = Signal(name="test_repr")
    check("repr format", "Signal(" in repr(sig))
    check("repr has name", "test_repr" in repr(sig))
    check("repr has receivers", "receivers=" in repr(sig))


def test_signal_thread_safety():
    """Test concurrent connect/disconnect from multiple threads."""
    import threading

    sig = Signal(name="threaded")
    errors = []

    def worker(thread_id):
        try:

            async def handler(sender, **kwargs):
                pass

            sig.connect(handler, dispatch_uid=f"thread_{thread_id}")
            sig.disconnect(dispatch_uid=f"thread_{thread_id}")
        except Exception as e:
            errors.append(f"Thread {thread_id}: {e}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    check("thread safety — no errors", len(errors) == 0, str(errors))


if __name__ == "__main__":
    sys.exit(main())
