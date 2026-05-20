"""
APIKey expiry timezone-normalization regression test (Finding N7).

# hyper-test: unit

Run: uv run hyper-test apikey_expiry

Background
----------
``SignedAPIKeyMixin.verify`` checks ``instance.expires_at`` against the
current time. The stored value may be:

    * a naive ISO string (no offset, e.g. "2030-01-01T00:00:00")
    * an aware ISO string (with offset, e.g. "2030-01-01T00:00:00+00:00")
    * a naive datetime object
    * an aware datetime object
    * None / "" (never expires)

Before the fix, a naive value produced a naive ``datetime`` and the
comparison ``datetime.now(UTC) > exp`` raised
``TypeError: can't compare offset-naive and offset-aware datetimes``
OUTSIDE the surrounding try — an uncaught exception → 500 on every
verification of such a key.

The fix normalizes ``exp`` to timezone-aware UTC (treating a naive stored
value as UTC) so the comparison can never raise, while still correctly
rejecting expired keys and accepting valid ones.

This test drives the REAL ``verify`` classmethod (via ``.__func__`` bound
to a lightweight fake ``cls``) so it exercises the exact production code
path — no DB, no network.
"""

import asyncio
import sys
from datetime import UTC, datetime, timedelta

from hyperdjango.signing import SignedAPIKeyMixin

_verify = SignedAPIKeyMixin.verify.__func__

FUTURE = datetime.now(UTC) + timedelta(days=30)
PAST = datetime.now(UTC) - timedelta(days=30)


class _FakeEngine:
    def decode_ref(self, signed):
        # Non-None => proceed to DB lookup / expiry check.
        return "reference-value"


class _FakeQuery:
    def __init__(self, instance):
        self._instance = instance

    async def first(self):
        return self._instance


class _FakeManager:
    def __init__(self, instance):
        self._instance = instance

    def filter(self, **kwargs):
        return _FakeQuery(self._instance)


class _FakeInstance:
    def __init__(self, expires_at):
        self.expires_at = expires_at
        self.is_active = True


def _make_cls(expires_at):
    """Build a minimal fake `cls` carrying everything verify() touches."""

    class _FakeAPIKey:
        _key_display_prefix = "sk_"
        _token_engine = _FakeEngine()
        objects = _FakeManager(_FakeInstance(expires_at))

    return _FakeAPIKey


def _verify_expires(expires_at):
    """Run the real verify() against a fake instance with this expires_at."""
    cls = _make_cls(expires_at)
    return asyncio.run(_verify(cls, "sk_anything"))


passed = 0
failed = 0
errors: list[str] = []


def check(name, condition):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        errors.append(name)
        print(f"  FAIL  {name}")


def check_valid(name, expires_at):
    """A valid (non-expired) key must return the instance, never raise."""
    try:
        result = _verify_expires(expires_at)
    except Exception as exc:  # noqa: BLE001 -- test must surface the raise
        failed_raise(name, exc)
        return
    check(name, result is not None)


def check_rejected(name, expires_at):
    """An expired key must return None (rejected), never raise."""
    try:
        result = _verify_expires(expires_at)
    except Exception as exc:  # noqa: BLE001 -- test must surface the raise
        failed_raise(name, exc)
        return
    check(name, result is None)


def failed_raise(name, exc):
    global failed
    failed += 1
    errors.append(f"{name}: raised {type(exc).__name__}: {exc}")
    print(f"  FAIL  {name}: raised {type(exc).__name__}: {exc}")


def main() -> int:
    print("=" * 70)
    print("APIKey expiry timezone normalization (Finding N7)")
    print("=" * 70)

    iso_naive = lambda d: d.replace(tzinfo=None).isoformat()  # noqa: E731
    iso_aware = lambda d: d.isoformat()  # noqa: E731

    # ── Naive ISO strings (the original 500 trigger) ────────────────────
    check_valid("naive-str future -> valid", iso_naive(FUTURE))
    check_rejected("naive-str past -> rejected", iso_naive(PAST))

    # ── Aware ISO strings ───────────────────────────────────────────────
    check_valid("aware-str future -> valid", iso_aware(FUTURE))
    check_rejected("aware-str past -> rejected", iso_aware(PAST))

    # ── Naive datetime objects ──────────────────────────────────────────
    check_valid("naive-datetime future -> valid", FUTURE.replace(tzinfo=None))
    check_rejected("naive-datetime past -> rejected", PAST.replace(tzinfo=None))

    # ── Aware datetime objects ──────────────────────────────────────────
    check_valid("aware-datetime future -> valid", FUTURE)
    check_rejected("aware-datetime past -> rejected", PAST)

    # ── No expiry ───────────────────────────────────────────────────────
    check_valid("None -> never expires", None)
    check_valid("empty-string -> never expires", "")

    # ── Corrupt expiry must FAIL CLOSED (rejected, not accepted) ─────────
    check_rejected("garbage-str -> rejected (fail closed)", "not-a-date")

    print()
    print("=" * 70)
    total = passed + failed
    print(f"Results: {passed}/{total} passed, {failed} failed")
    if errors:
        print("\nFailures:")
        for e in errors:
            print(f"  {e}")
    print("=" * 70)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
