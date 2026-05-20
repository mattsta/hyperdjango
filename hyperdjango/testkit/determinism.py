"""Deterministic waiting and tamper helpers for timing-sensitive tests.

These replace ad-hoc versions that caused real CI flakes: a fixed ``sleep``
before asserting a converged metric (raced under CPU starvation), and an
"append X" token tamper that was a no-op whenever the token already ended in
``X`` — a ~1-in-62 false pass. The helpers here are convergence-based and
never no-op.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable

_DEFAULT_INTERVAL_S = 0.01

# tamper cycles the last character to its neighbour within its own alphabet
# class; a class's final character wraps to the first so the result stays in
# class yet always differs from the input.
_STEP = 1
_DIGIT_LO, _DIGIT_HI = "0", "9"
_LOWER_LO, _LOWER_HI = "a", "z"
_UPPER_LO, _UPPER_HI = "A", "Z"
_SYMBOL_A, _SYMBOL_B = ".", "_"
_EMPTY_REPLACEMENT = "x"


def wait_until(
    pred: Callable[[], object],
    timeout_s: float,
    interval_s: float = _DEFAULT_INTERVAL_S,
    desc: str = "",
) -> None:
    """Block until ``pred()`` is truthy, polling every ``interval_s`` seconds.

    Raises :class:`TimeoutError` mentioning ``desc`` and the elapsed time once
    ``timeout_s`` passes without ``pred`` becoming truthy.
    """
    start = time.monotonic()
    deadline = start + timeout_s
    while True:
        if pred():
            return
        now = time.monotonic()
        if now >= deadline:
            label = f" [{desc}]" if desc else ""
            raise TimeoutError(f"wait_until{label} timed out after {now - start:.3f}s")
        time.sleep(interval_s)


async def await_until(
    pred: Callable[[], object | Awaitable[object]],
    timeout_s: float,
    interval_s: float = _DEFAULT_INTERVAL_S,
    desc: str = "",
) -> None:
    """Async twin of :func:`wait_until`, sleeping with :func:`asyncio.sleep`.

    ``pred`` may be synchronous or return an awaitable; an awaitable result is
    awaited each poll. Raises :class:`TimeoutError` mentioning ``desc`` and the
    elapsed time on expiry.
    """
    start = time.monotonic()
    deadline = start + timeout_s
    while True:
        result = pred()
        if inspect.isawaitable(result):
            result = await result
        if result:
            return
        now = time.monotonic()
        if now >= deadline:
            label = f" [{desc}]" if desc else ""
            raise TimeoutError(f"await_until{label} timed out after {now - start:.3f}s")
        await asyncio.sleep(interval_s)


def tamper(token: str) -> str:
    """Return a string that is ALWAYS different from ``token``.

    The last character is cycled to its neighbour within its own alphabet class
    (digit, lowercase, uppercase, or symbol), wrapping the class's final
    character back to the first. The class is preserved so a tampered signed
    token stays structurally valid (e.g. still base62) while its decoded bytes
    differ — the exact property a rejection test needs. An empty input yields a
    fixed non-empty result.
    """
    if not token:
        return _EMPTY_REPLACEMENT
    last = token[-1]
    if last.isdigit():
        repl = _DIGIT_LO if last == _DIGIT_HI else chr(ord(last) + _STEP)
    elif last.islower():
        repl = _LOWER_LO if last == _LOWER_HI else chr(ord(last) + _STEP)
    elif last.isupper():
        repl = _UPPER_LO if last == _UPPER_HI else chr(ord(last) + _STEP)
    else:
        repl = _SYMBOL_B if last == _SYMBOL_A else _SYMBOL_A
    return token[:-1] + repl
