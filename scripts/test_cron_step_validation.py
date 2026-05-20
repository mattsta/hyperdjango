"""Cron step-value validation for `_cron_to_scheduler_timing` (finding N9).

A malformed step of 0 (e.g. "*/0") in the hour or minute field must be
rejected at parse time. Before the fix, the HOUR field lacked the
`step > 0` guard that the minute field already had, so "*/0" in the hour
position produced ("cyclic", timedelta(hours=0)) — a zero-length interval
that busy-loops the scheduler when it fires "every 0 hours". A negative
step ("*/-1") likewise produced a negative/garbage interval.

This test asserts:
  * "*/0" and "*/-1" in the hour field raise a clear ValueError.
  * "*/0" and "*/-1" in the minute field raise a clear ValueError.
  * Valid steps ("*/2", "0 */3 * * *") expand to the correct cyclic interval.

Every parse call runs under a SIGALRM watchdog so that a *regression* which
reintroduced an actual loop (rather than a bad-but-fast return value) fails
the test instead of hanging the suite.

Run:  uv run hyper-test cron_step_validation
"""

# hyper-test: unit

import datetime
import signal
from contextlib import contextmanager

from hyperdjango.tasks import _cron_to_scheduler_timing

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


class _Timeout(Exception):
    pass


@contextmanager
def watchdog(seconds: int = 3):
    """Abort a call that runs longer than `seconds` (catches infinite loops)."""

    def _handler(signum, frame):
        raise _Timeout(f"call exceeded {seconds}s -- possible infinite loop")

    old = signal.signal(signal.SIGALRM, _handler)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def _expect_reject(name: str, expr: str) -> None:
    """A bad step must raise ValueError promptly (not hang, not return garbage)."""
    try:
        with watchdog():
            result = _cron_to_scheduler_timing(expr)
    except ValueError as e:
        check(name, True)
        check(
            f"{name}: message mentions the offending step",
            "step" in str(e).lower(),
            repr(str(e)),
        )
    except _Timeout as e:
        check(name, False, f"HUNG: {e}")
    else:
        check(name, False, f"expected ValueError, got {result!r}")


def _expect_cyclic(name: str, expr: str, expected: datetime.timedelta) -> None:
    try:
        with watchdog():
            method, arg = _cron_to_scheduler_timing(expr)
    except _Timeout as e:
        check(name, False, f"HUNG: {e}")
        return
    check(name, method == "cyclic" and arg == expected, f"got ({method!r}, {arg!r})")


def test_hour_step_zero_rejected() -> None:
    print("\n=== hour field: */0 rejected ===")
    _expect_reject("hour */0", "0 */0 * * *")
    _expect_reject("hour */-1 (negative)", "0 */-1 * * *")


def test_minute_step_zero_rejected() -> None:
    print("\n=== minute field: */0 rejected ===")
    _expect_reject("minute */0", "*/0 * * * *")
    _expect_reject("minute */-1 (negative)", "*/-1 * * * *")


def test_valid_steps_expand() -> None:
    print("\n=== valid steps expand correctly ===")
    _expect_cyclic("minute */2", "*/2 * * * *", datetime.timedelta(minutes=2))
    _expect_cyclic("hour */3", "0 */3 * * *", datetime.timedelta(hours=3))
    _expect_cyclic("hour */1", "0 */1 * * *", datetime.timedelta(hours=1))


def run() -> bool:
    test_hour_step_zero_rejected()
    test_minute_step_zero_rejected()
    test_valid_steps_expand()
    print(f"\n{'=' * 60}")
    print(f"Results: {_PASS} passed, {_FAIL} failed")
    print(f"{'=' * 60}")
    return _FAIL == 0


if __name__ == "__main__":
    import sys

    sys.exit(0 if run() else 1)
