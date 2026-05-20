"""
End-to-end propagation: setting → configure_from_settings → live ring.

# hyper-test: unit

The previous test files cover:
  - test_span_ring_configure.py: manual `_span_configure(N)` → init at N
  - test_telemetry_settings.py: configure_from_settings returns a bootstrap

Neither one proves that the FULL chain works:

    `TELEMETRY_SPAN_RING_CAPACITY` setting
        → conf.get_setting()
        → telemetry.setup.configure_from_settings()
        → _span_configure(N)
        → first start() triggers init at N
        → live ring is at N

This file IS that test. Runs in its own subprocess (every hyper-test
script does) so the Zig module starts fresh and the ring hasn't been
touched yet — letting us prove the setting actually takes effect.
"""

import sys
from unittest.mock import patch

from hyperdjango._hyperdjango_native import (
    _span_capacity,
    _span_drain,
    _span_end,
    _span_is_operational,
    _span_start,
)

from hyperdjango.conf import DEFAULTS
from hyperdjango.telemetry import disable
from hyperdjango.telemetry.setup import configure_from_settings

# Use a tiny non-default capacity so the test absolutely proves the
# setting was honored — there's no way the default 16384 would
# accidentally match.
TARGET_CAPACITY = 1024


passed = 0
failed = 0
errors: list[str] = []


def check(name: str, cond: bool, msg: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        err = f"FAIL: {name}"
        if msg:
            err += f" — {msg}"
        errors.append(err)
        print(f"  {err}")


def test_pre_init_baseline() -> None:
    """Sanity: before any setup runs, the ring is at default and
    not yet operational."""
    print("\n── pre-init baseline ──")
    check("not operational before init", _span_is_operational() is False)
    check("capacity is default 16384", _span_capacity() == 16384)


def test_setting_propagates_to_live_ring() -> None:
    """The full chain: setting → configure_from_settings →
    _span_configure → first span → live ring at target capacity.
    """
    print(f"\n── setting → configure_from_settings → ring at {TARGET_CAPACITY} ──")
    overrides = {
        "TELEMETRY_ENABLED": True,
        "TELEMETRY_SINKS": ["memory"],
        "TELEMETRY_DRAIN_INTERVAL": 0.05,
        "TELEMETRY_SPAN_RING_CAPACITY": TARGET_CAPACITY,
    }
    bootstrap = None
    try:
        with patch.dict(DEFAULTS, overrides):
            bootstrap = configure_from_settings()
        check("bootstrap built", bootstrap is not None)

        # The configure_from_settings call should have set the
        # configured_capacity but NOT yet allocated the ring (no spans
        # started). _span_capacity() returns configured value.
        check(
            f"capacity now {TARGET_CAPACITY} (configured, not yet live)",
            _span_capacity() == TARGET_CAPACITY,
            f"got {_span_capacity()}",
        )
        check(
            "still not operational (no spans started yet)",
            _span_is_operational() is False,
        )

        # Now start a span — this triggers init() which should
        # allocate at the configured capacity.
        h = _span_start(0, 1, 0, "trigger", True)
        check("first span got non-sentinel handle", h != 0)
        _span_end(h)
        _span_drain()

        # After init, the ring should be operational at TARGET_CAPACITY
        check(
            "ring is operational after first span",
            _span_is_operational() is True,
        )
        check(
            f"live capacity is {TARGET_CAPACITY}",
            _span_capacity() == TARGET_CAPACITY,
            f"got {_span_capacity()}",
        )
    finally:
        if bootstrap is not None:
            bootstrap.middleware.shutdown()
        disable()


def test_subsequent_call_with_same_setting_is_noop() -> None:
    """Calling configure_from_settings again with the same capacity
    should be a no-op (no warning, no error). The check
    `if span_ring_capacity != _span_capacity()` short-circuits.
    """
    print("\n── second call with same capacity is no-op ──")
    overrides = {
        "TELEMETRY_ENABLED": True,
        "TELEMETRY_SINKS": ["memory"],
        "TELEMETRY_DRAIN_INTERVAL": 0.05,
        "TELEMETRY_SPAN_RING_CAPACITY": TARGET_CAPACITY,
    }
    bootstrap = None
    try:
        with patch.dict(DEFAULTS, overrides):
            bootstrap = configure_from_settings()
        check("bootstrap built", bootstrap is not None)
        check(
            "live capacity unchanged",
            _span_capacity() == TARGET_CAPACITY,
        )
        check(
            "still operational",
            _span_is_operational() is True,
        )
    finally:
        if bootstrap is not None:
            bootstrap.middleware.shutdown()
        disable()


def test_subsequent_call_with_different_setting_warns() -> None:
    """Calling configure_from_settings with a DIFFERENT capacity
    after the ring is live should emit a warning (cannot reconfigure
    a live ring) but NOT raise — bootstrap still succeeds.
    """
    print("\n── second call with different capacity warns ──")
    overrides = {
        "TELEMETRY_ENABLED": True,
        "TELEMETRY_SINKS": ["memory"],
        "TELEMETRY_DRAIN_INTERVAL": 0.05,
        # Intentionally different from the live TARGET_CAPACITY
        "TELEMETRY_SPAN_RING_CAPACITY": TARGET_CAPACITY * 2,
    }
    bootstrap = None
    try:
        with patch.dict(DEFAULTS, overrides):
            bootstrap = configure_from_settings()
        check("bootstrap returned despite mismatch", bootstrap is not None)
        check(
            f"live capacity still {TARGET_CAPACITY} (unchanged)",
            _span_capacity() == TARGET_CAPACITY,
        )
        check("still operational", _span_is_operational() is True)
    finally:
        if bootstrap is not None:
            bootstrap.middleware.shutdown()
        disable()


def test_invalid_setting_raises_at_bootstrap() -> None:
    """A non-power-of-2 setting passes the conf.py min/max validation
    (which only checks integer range) but fails at the Zig FFI layer.
    The setup helper re-raises ValueError so the user sees a hard
    error at boot rather than silent fallback.

    Note: this test uses a non-power-of-2 value AND relies on the
    fact that the live ring is at TARGET_CAPACITY != 1023, so the
    diff check triggers the configure call.
    """
    print("\n── invalid setting raises ValueError at bootstrap ──")
    overrides = {
        "TELEMETRY_ENABLED": True,
        "TELEMETRY_SINKS": ["memory"],
        "TELEMETRY_DRAIN_INTERVAL": 0.05,
        "TELEMETRY_SPAN_RING_CAPACITY": 1023,  # not a power of 2
    }
    bootstrap = None
    raised = None
    try:
        with patch.dict(DEFAULTS, overrides):
            bootstrap = configure_from_settings()
    except ValueError as exc:
        raised = exc
    finally:
        if bootstrap is not None:
            bootstrap.middleware.shutdown()
        disable()
    check("ValueError raised", isinstance(raised, ValueError))
    if raised is not None:
        check(
            "error mentions power of 2",
            "power of 2" in str(raised),
            f"got {raised!r}",
        )


def main() -> int:
    print("=" * 70)
    print("  Span ring settings propagation (audit round 3)")
    print("=" * 70)

    test_pre_init_baseline()
    test_setting_propagates_to_live_ring()
    test_subsequent_call_with_same_setting_is_noop()
    test_subsequent_call_with_different_setting_warns()
    test_invalid_setting_raises_at_bootstrap()

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
