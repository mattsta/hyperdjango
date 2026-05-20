"""SafeLazy — the ONE thread-safe lazy-singleton primitive.

Free-threaded CPython 3.14t has no GIL, so the hand-rolled double-checked-locking
singletons scattered across the codebase (DB executor, WS executor/pool, default
cache, …) each re-implemented the same 8-line pattern — and the one place a copy
drifted reproduced a real partial-publish race (the round-13 class, see the native
where_compiler/db.zig fixes). This centralizes the correct pattern in one audited
place so it can't drift again.

Correctness: the built value is stored in a SINGLE reference slot, published with
one store AFTER the factory has fully returned. A reader's single unlocked load
therefore observes either the UNSET sentinel or the fully-built object — never a
half-built one (mirrors cache.py's atomic-tuple-swap discipline). The factory runs
exactly once, under the lock, with a re-check.
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass, field

_UNSET = object()


@dataclass(slots=True)
class SafeLazy[T]:
    """A lazily-built, thread-safe singleton value.

    Usage:
        _executor = SafeLazy(lambda: ThreadPoolExecutor(max_workers=8))
        _executor.get()   # builds once, then returns the cached value
    """

    _factory: Callable[[], T]
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _value: object = field(default=_UNSET)

    def get(self) -> T:
        # Fast path: one unlocked load. Sees UNSET or the fully-built value.
        v = self._value
        if v is not _UNSET:
            return v  # type: ignore[return-value]
        with self._lock:
            if self._value is _UNSET:
                # Build fully, THEN publish with a single store.
                self._value = self._factory()
            return self._value  # type: ignore[return-value]

    @property
    def built(self) -> bool:
        """True if the value has been built (for shutdown/introspection)."""
        return self._value is not _UNSET

    def peek(self) -> T | None:
        """The built value, or None if not yet built (never triggers a build)."""
        v = self._value
        return None if v is _UNSET else v  # type: ignore[return-value]

    def reset(self) -> None:
        """Drop the built value so the next get() rebuilds. For shutdown/tests."""
        with self._lock:
            self._value = _UNSET
