"""
Signal/event system for lifecycle hooks.

Async-first signal dispatch inspired by Django's django.dispatch.

Usage:
    from hyperdjango.signals import Signal

    # Define a signal
    order_placed = Signal()

    # Connect handlers
    @order_placed.connect
    async def send_confirmation(sender, **kwargs):
        order = kwargs["order"]
        print(f"Sending confirmation for order {order.id}")

    # Or connect without decorator
    async def update_inventory(sender, **kwargs):
        ...
    order_placed.connect(update_inventory)

    # Send the signal
    await order_placed.send(sender=OrderView, order=order)

Built-in signals:
    from hyperdjango.signals import (
        pre_save, post_save, pre_delete, post_delete,
        user_logged_in, user_logged_out, user_login_failed,
        request_started, request_finished,
    )
"""

import inspect
import logging
import threading
import weakref
from collections.abc import Callable
from typing import Any

_logger = logging.getLogger("hyperdjango.signals")


def log_robust_responses(
    responses: list[tuple[Callable, Any]],
    logger: logging.Logger,
    signal_name: str = "",
) -> int:
    """Log any receiver failures collected by :meth:`Signal.send_robust`.

    ``send_robust`` never raises; instead each failing receiver's exception is
    captured as its response. Post-commit callers pass the returned response
    list here to surface those failures LOUDLY at error level (with traceback),
    so a raising receiver is observable and never silent.

    Args:
        responses: the list returned by ``send_robust``.
        logger: the caller's module logger to emit under.
        signal_name: signal name, for context in the log record.

    Returns:
        The number of failed receivers logged.
    """
    failures = 0
    for receiver, response in responses:
        if isinstance(response, Exception):
            failures += 1
            logger.error(
                "signal %r receiver %r raised %s",
                signal_name,
                receiver,
                type(response).__name__,
                exc_info=response,
            )
    return failures


def _make_id(receiver: Callable):
    """Return a stable identity key for a receiver.

    A fresh bound method is a new object on every attribute access, so
    `id(receiver)` differs each time — disconnect()/dedup would never
    match, leaking receivers and double-firing, and under free-threading a
    recycled address could even collide with an unrelated receiver. Key
    bound methods by (id(instance), id(function)) — stable for the lifetime
    of the underlying object — mirroring Django's `_make_id`.
    """
    if inspect.ismethod(receiver):
        return (id(receiver.__self__), id(receiver.__func__))
    return id(receiver)


class Signal:
    """Async-capable signal that dispatches to connected receivers.

    Receivers can be sync or async functions. Async receivers are awaited,
    sync receivers are called directly.

    Thread-safe: receivers list is protected by a lock.
    """

    def __init__(self, name: str = ""):
        self.name = name
        # Source of truth: (key, stored, is_async). `stored` is the receiver or
        # a weakref; `is_async` is resolved once at connect() time.
        self._receivers: list[tuple[int, Callable, bool]] = []
        self._lock = threading.Lock()
        # Copy-on-write snapshot read locklessly by send(). Packed atomically as
        # (has_weak, entries) so a single attribute read is always consistent:
        # when has_weak is False the entries are (receiver, is_async) pairs that
        # send() uses directly — no per-send list rebuild, no weakref scan.
        self._snapshot: tuple[bool, tuple[tuple[Callable, bool], ...]] = (False, ())

    def _rebuild_snapshot(self) -> None:
        """Rebuild the copy-on-write receiver snapshot. Must hold self._lock."""
        entries = tuple((stored, is_async) for _, stored, is_async in self._receivers)
        has_weak = any(
            isinstance(stored, (weakref.ref, weakref.WeakMethod))
            for stored, _ in entries
        )
        # Single atomic assignment — readers never observe a torn snapshot.
        self._snapshot = (has_weak, entries)

    def connect(
        self,
        receiver: Callable = None,
        *,
        weak: bool = False,
        dispatch_uid: str | None = None,
    ) -> Callable:
        """Connect a receiver function to this signal.

        Can be used as a decorator or called directly:
            @my_signal.connect
            async def handler(sender, **kwargs): ...

            my_signal.connect(handler)

        Args:
            receiver: The function to call when the signal is sent.
            weak: If True, store a weak reference (auto-disconnect on GC).
            dispatch_uid: Unique ID to prevent duplicate connections.

        Returns:
            The receiver function (for decorator use).
        """
        if receiver is None:
            # Used as @signal.connect with keyword args
            def decorator(fn):
                self.connect(fn, weak=weak, dispatch_uid=dispatch_uid)
                return fn

            return decorator

        lookup_key = dispatch_uid or _make_id(receiver)
        is_async = inspect.iscoroutinefunction(receiver)

        with self._lock:
            # Check for duplicate
            for existing_key, _, _ in self._receivers:
                if existing_key == lookup_key:
                    return receiver

            if weak and hasattr(receiver, "__self__"):
                # Weak reference for bound methods
                ref = weakref.WeakMethod(receiver, self._make_cleanup(lookup_key))
                self._receivers.append((lookup_key, ref, is_async))
            elif weak:
                ref = weakref.ref(receiver, self._make_cleanup(lookup_key))
                self._receivers.append((lookup_key, ref, is_async))
            else:
                self._receivers.append((lookup_key, receiver, is_async))
            self._rebuild_snapshot()

        return receiver

    def disconnect(
        self, receiver: Callable = None, *, dispatch_uid: str | None = None
    ) -> bool:
        """Disconnect a receiver from this signal.

        Returns True if a receiver was disconnected, False if not found.
        """
        lookup_key = dispatch_uid or _make_id(receiver)

        with self._lock:
            for i, (key, _, _) in enumerate(self._receivers):
                if key == lookup_key:
                    self._receivers.pop(i)
                    self._rebuild_snapshot()
                    return True
        return False

    async def send(self, sender: Any = None, **kwargs) -> list[tuple[Callable, Any]]:
        """Send the signal to all connected receivers, propagating failures.

        Fail-fast contract: receivers are
        invoked in connection order and the FIRST receiver to raise ABORTS
        the dispatch — the exception propagates to the caller and no further
        receivers run. This lets a pre-commit / validating receiver veto the
        operation that fired the signal (e.g. a ``pre_save`` handler rejecting
        an invalid instance).

        For a variant that never propagates and always calls every receiver,
        use :meth:`send_robust`.

        Args:
            sender: The object sending the signal (typically a class or instance).
            **kwargs: Keyword arguments passed to each receiver.

        Returns:
            List of ``(receiver, return_value)`` tuples, in connection order —
            returned only when EVERY receiver returned normally.

        Raises:
            Exception: whatever the first failing receiver raised.
        """
        responses = []
        receivers = self._live_receivers()

        for receiver, is_async in receivers:
            if is_async:
                response = await receiver(sender, **kwargs)
            else:
                response = receiver(sender, **kwargs)
            responses.append((receiver, response))

        return responses

    async def send_robust(
        self, sender: Any = None, **kwargs
    ) -> list[tuple[Callable, Any]]:
        """Send the signal, catching every receiver exception.

        Robust contract: every
        receiver is invoked even if earlier ones raise, and this method NEVER
        propagates — a failing receiver's exception is captured as its
        response instead of being raised. Use this for post-commit /
        notification signals (``post_save``, ``post_delete``, ...) where a
        receiver failure must not abort an already-committed operation.

        Unlike :meth:`send`, a captured exception is NOT silent to the
        program: it is returned in the response list so the caller can log or
        react to it. See :func:`log_robust_responses`.

        Returns:
            List of ``(receiver, return_value_or_exception)`` tuples, in
            connection order. A tuple whose second element is an ``Exception``
            marks a receiver that failed.
        """
        responses = []
        receivers = self._live_receivers()

        for receiver, is_async in receivers:
            try:
                if is_async:
                    response = await receiver(sender, **kwargs)
                else:
                    response = receiver(sender, **kwargs)
                responses.append((receiver, response))
            # blind-except: send_robust's documented contract ISOLATES each receiver — one failure must neither abort the others nor propagate to the already-committed caller; the exception is captured as the receiver's response for the caller to log (never silent — see log_robust_responses).
            except Exception as e:
                responses.append((receiver, e))

        return responses

    def has_receivers(self) -> bool:
        """Return True if any receivers are connected."""
        return len(self._live_receivers()) > 0

    @property
    def receiver_count(self) -> int:
        """Number of connected receivers."""
        return len(self._live_receivers())

    def _live_receivers(self) -> list[tuple[Callable, bool]]:
        """Return list of live (receiver, is_async) pairs, resolving weak refs.

        Reads the copy-on-write snapshot locklessly. When no weak receivers are
        connected the snapshot entries are returned verbatim — no liveness scan
        and no lock. Dead weak refs are pruned eagerly by the GC cleanup
        callback, so here we merely skip any that expired since the snapshot.
        """
        has_weak, entries = self._snapshot
        if not has_weak:
            # Fast path: every entry is a strong (receiver, is_async) pair.
            return list(entries)

        result: list[tuple[Callable, bool]] = []
        for stored, is_async in entries:
            if isinstance(stored, (weakref.ref, weakref.WeakMethod)):
                strong = stored()
                if strong is not None:
                    result.append((strong, is_async))
            else:
                result.append((stored, is_async))
        return result

    def _make_cleanup(self, lookup_key):
        """Create a weak reference cleanup callback."""

        def cleanup(ref):
            with self._lock:
                self._receivers = [
                    (k, r, a) for k, r, a in self._receivers if k != lookup_key
                ]
                self._rebuild_snapshot()

        return cleanup

    def __repr__(self):
        return f"Signal({self.name!r}, receivers={self.receiver_count})"


# ---------------------------------------------------------------------------
# Built-in signals
# ---------------------------------------------------------------------------

# Model lifecycle
pre_save = Signal(name="pre_save")
post_save = Signal(name="post_save")
pre_delete = Signal(name="pre_delete")
post_delete = Signal(name="post_delete")

# Auth lifecycle
user_logged_in = Signal(name="user_logged_in")
user_logged_out = Signal(name="user_logged_out")
user_login_failed = Signal(name="user_login_failed")

# Request lifecycle
request_started = Signal(name="request_started")
request_finished = Signal(name="request_finished")
