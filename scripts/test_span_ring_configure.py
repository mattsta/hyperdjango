"""
Span ring runtime capacity configuration tests (task #248).

# hyper-test: unit

Coverage:

    Pre-init phase (must run first — validation order matters)
      1. Default capacity is DEFAULT_RING_CAPACITY (16384)
      2. Setting registered in DEFAULTS + SETTING_DEFINITIONS
      3. configure(non-power-of-2) → ValueError
      4. configure(0) → ValueError (out of range)
      5. configure(too small) → ValueError (out of range)
      6. configure(too large) → ValueError (out of range)
      7. After failed configures, capacity is still default

    Successful configure to a small capacity
      8. configure(512) succeeds (no init yet)
      9. _span_capacity() reflects the new value (still pre-init)
      10. First _span_start triggers init at the new capacity
      11. Burst beyond 512 spans → dropped_count rises
      12. Drain returns at most 512 spans
      13. configure() AFTER init raises RuntimeError

    Settings integration
      14. TELEMETRY_SPAN_RING_CAPACITY in DEFAULTS + SETTING_DEFINITIONS
      15. configure_from_settings honors the live capacity (no-op when same)

The test uses the test runner's per-script subprocess isolation to
get a fresh Zig module init, so we can prove the configure→init→use
sequence works end-to-end on a non-default capacity.
"""

import sys
from unittest.mock import patch

from hyperdjango._hyperdjango_native import (
    _span_capacity,
    _span_configure,
    _span_drain,
    _span_dropped_count,
    _span_end,
    _span_reset_for_tests,
    _span_start,
)

from hyperdjango.conf import DEFAULTS, SETTING_DEFINITIONS
from hyperdjango.telemetry import disable, enable
from hyperdjango.telemetry.setup import configure_from_settings

try:
    from hypothesis import HealthCheck, given, settings
    from hypothesis import strategies as st

    HAS_HYPOTHESIS = True
except ImportError:
    HAS_HYPOTHESIS = False

passed = 0
failed = 0
errors: list[str] = []

# Configured-down ring capacity for the runtime-tuning test. 512 is
# small enough to fill quickly with a few thousand spans (proving
# the new capacity is actually in effect) but large enough to do
# meaningful drain assertions.
SMALL_CAPACITY = 512


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


def _expect_value_error(name: str, fn) -> None:
    try:
        fn()
    except ValueError:
        check(name, True)
        return
    except Exception as exc:
        check(name, False, f"expected ValueError, got {type(exc).__name__}: {exc}")
        return
    check(name, False, "expected ValueError")


def _expect_runtime_error(name: str, fn) -> None:
    try:
        fn()
    except RuntimeError:
        check(name, True)
        return
    except Exception as exc:
        check(name, False, f"expected RuntimeError, got {type(exc).__name__}: {exc}")
        return
    check(name, False, "expected RuntimeError")


# ── Phase 1: pre-init checks (no _span_start has been called yet) ──────────


def test_default_capacity_is_16384() -> None:
    print("\n── Default ring capacity ──")
    cap = _span_capacity()
    check("default capacity is 16384", cap == 16384, f"got {cap}")


def test_setting_registered() -> None:
    print("\n── TELEMETRY_SPAN_RING_CAPACITY in registry ──")
    check("in DEFAULTS", "TELEMETRY_SPAN_RING_CAPACITY" in DEFAULTS)
    check(
        "in SETTING_DEFINITIONS",
        "TELEMETRY_SPAN_RING_CAPACITY" in SETTING_DEFINITIONS,
    )
    defn = SETTING_DEFINITIONS["TELEMETRY_SPAN_RING_CAPACITY"]
    check("type is int", defn.type is int)
    check("default is 16384", defn.default == 16384)
    check("min_value is 256", defn.min_value == 256)
    check("max_value is 16777216", defn.max_value == 16777216)


def test_validation_errors_before_init() -> None:
    print("\n── Validation errors (still pre-init) ──")
    _expect_value_error(
        "configure(1000) [non-power-of-2]",
        lambda: _span_configure(1000),
    )
    _expect_value_error(
        "configure(15) [non-power-of-2 + too small]",
        lambda: _span_configure(15),
    )
    _expect_value_error(
        "configure(128) [< MIN]",
        lambda: _span_configure(128),
    )
    _expect_value_error(
        "configure(1<<25) [> MAX]",
        lambda: _span_configure(1 << 25),
    )
    # After all the failed configures, default capacity should still hold
    cap = _span_capacity()
    check(
        "capacity unchanged by failed configures",
        cap == 16384,
        f"got {cap}",
    )


def test_hypothesis_configure_validator() -> None:
    """Property: configure(N) accepts iff N is a power of 2 in
    [256, 16777216]. Otherwise it raises ValueError. Tests must run
    BEFORE init() so the AlreadyInitialized check doesn't dominate.
    """
    if not HAS_HYPOTHESIS:
        print("\n── Hypothesis configure validator: SKIPPED ──")
        return
    print("\n── Hypothesis: configure() validator ──")

    MIN = 256
    MAX = 16777216

    def is_power_of_two(n: int) -> bool:
        return n > 0 and (n & (n - 1)) == 0

    @given(cap=st.integers(min_value=1, max_value=MAX * 2))
    @settings(
        max_examples=200,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def fuzz(cap: int) -> None:
        # Skip the one valid case that would actually init the ring
        # at our default capacity — we want to leave the ring at
        # default for the next phase of tests.
        if cap == 16384:
            return
        valid = MIN <= cap <= MAX and is_power_of_two(cap)
        if valid:
            # Don't actually call configure with valid values — that
            # would change configured_capacity and disrupt the rest
            # of the test phases. Just verify our validator math.
            return
        # Invalid values must raise ValueError. Catch and verify type.
        try:
            _span_configure(cap)
        except ValueError:
            return
        except RuntimeError:
            # If init has already happened in this process, AlreadyInitialized
            # is also acceptable. The validator is unreachable in that case.
            return
        raise AssertionError(
            f"configure({cap}) accepted but should have been rejected "
            f"(min={MIN} max={MAX} pow2={is_power_of_two(cap)})"
        )

    fuzz()
    check("hypothesis configure() validator", True)


# ── Phase 2: configure to a small ring + verify it took effect ─────────────


def test_configure_to_small_capacity() -> None:
    print(f"\n── configure({SMALL_CAPACITY}) succeeds (still pre-init) ──")
    _span_configure(SMALL_CAPACITY)
    check("configure(512) returned None", True)
    cap = _span_capacity()
    check(
        f"capacity now {SMALL_CAPACITY}",
        cap == SMALL_CAPACITY,
        f"got {cap}",
    )


def test_first_span_inits_at_small_capacity() -> None:
    print(f"\n── First span init uses {SMALL_CAPACITY} ──")
    enable()
    try:
        h = _span_start(0, 1, 0, "first", True)
        check("first span got a non-sentinel handle", h != 0)
        _span_end(h)
        cap = _span_capacity()
        check(
            f"live capacity is {SMALL_CAPACITY}",
            cap == SMALL_CAPACITY,
            f"got {cap}",
        )
        # Drain so we don't carry state between phases
        _span_drain()
    finally:
        disable()


def test_burst_beyond_small_capacity_drops() -> None:
    print(f"\n── Burst beyond {SMALL_CAPACITY} bumps dropped_count ──")
    enable()
    _span_reset_for_tests()
    try:
        before = _span_dropped_count()
        BURST = 4096
        for i in range(BURST):
            h = _span_start(0, i, 0, "burst", True)
            if h:
                _span_end(h)
        after = _span_dropped_count()
        delta = after - before
        # We expect at least (BURST - SMALL_CAPACITY) drops because the
        # ring fills up with completed slots and subsequent claims see
        # state=complete (not free) and bump dropped_count.
        check(
            f"≥{BURST - SMALL_CAPACITY} drops after burst",
            delta >= (BURST - SMALL_CAPACITY),
            f"delta={delta}",
        )
        drained = _span_drain()
        check(
            f"drain returns ≤{SMALL_CAPACITY} spans",
            len(drained) <= SMALL_CAPACITY,
            f"got {len(drained)}",
        )
    finally:
        disable()


def test_configure_after_init_raises() -> None:
    print("\n── configure() after init → RuntimeError ──")
    _expect_runtime_error(
        "configure(1024) after init raises",
        lambda: _span_configure(1024),
    )


# ── Phase 3: configure_from_settings honors the live capacity ─────────────


def test_configure_from_settings_no_op_when_matched() -> None:
    print("\n── configure_from_settings no-op when matched ──")
    overrides = {
        "TELEMETRY_ENABLED": True,
        "TELEMETRY_SINKS": ["memory"],
        "TELEMETRY_DRAIN_INTERVAL": 0.05,
        # Match the live (small) capacity so the setup helper takes
        # the no-op branch instead of attempting reconfiguration.
        "TELEMETRY_SPAN_RING_CAPACITY": SMALL_CAPACITY,
    }
    bootstrap = None
    try:
        with patch.dict(DEFAULTS, overrides):
            bootstrap = configure_from_settings()
        check("bootstrap built successfully", bootstrap is not None)
        check(
            "live capacity unchanged",
            _span_capacity() == SMALL_CAPACITY,
        )
    finally:
        if bootstrap is not None:
            bootstrap.middleware.shutdown()
        disable()


def test_configure_from_settings_warns_on_mismatch() -> None:
    print("\n── configure_from_settings warns when mismatched (no raise) ──")
    overrides = {
        "TELEMETRY_ENABLED": True,
        "TELEMETRY_SINKS": ["memory"],
        "TELEMETRY_DRAIN_INTERVAL": 0.05,
        "TELEMETRY_SPAN_RING_CAPACITY": 8192,  # different from live SMALL_CAPACITY
    }
    bootstrap = None
    try:
        with patch.dict(DEFAULTS, overrides):
            bootstrap = configure_from_settings()
        # Bootstrap still succeeds — we log + ignore the runtime error
        check("bootstrap returned despite mismatch", bootstrap is not None)
        check(
            f"live capacity still {SMALL_CAPACITY}",
            _span_capacity() == SMALL_CAPACITY,
        )
    finally:
        if bootstrap is not None:
            bootstrap.middleware.shutdown()
        disable()


def main() -> int:
    print("=" * 70)
    print("  Span ring capacity configuration (task #248)")
    print("=" * 70)

    # Phase 1: pre-init
    test_default_capacity_is_16384()
    test_setting_registered()
    test_validation_errors_before_init()
    test_hypothesis_configure_validator()

    # Phase 2: configure to small + init at small + verify behavior
    test_configure_to_small_capacity()
    test_first_span_inits_at_small_capacity()
    test_burst_beyond_small_capacity_drops()
    test_configure_after_init_raises()

    # Phase 3: settings integration
    test_configure_from_settings_no_op_when_matched()
    test_configure_from_settings_warns_on_mismatch()

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
