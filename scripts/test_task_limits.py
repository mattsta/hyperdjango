"""
Unit tests for task queue per-user limits and circuit breaker.

# hyper-test: unit
"""

import asyncio
import time

from hyperdjango.tasks import (
    CircuitBreakerState,
    CircuitState,
    TaskCircuitOpenError,
    TaskQueue,
    TaskUserLimitError,
)

PASS = 0
FAIL = 0
ERRORS: list[str] = []


def ok(name: str, cond: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
        return True
    FAIL += 1
    msg = f"  FAIL  {name}" + (f" — {detail}" if detail else "")
    print(msg)
    ERRORS.append(msg)
    return False


import contextlib
import threading

_gate = threading.Event()


async def _noop():
    pass


async def _blocking():
    """Block until the gate is set — used to keep tasks pending."""
    while not _gate.is_set():
        await asyncio.sleep(0.01)


async def _always_fail():
    raise ValueError("intentional")


def main() -> None:
    global PASS, FAIL

    print("=" * 60)
    print("Task Queue: Per-User Limits + Circuit Breaker")
    print("=" * 60)

    # ── Per-User Limits ─────────────────────────────────────
    print("\n--- Per-User Limits ---")

    # Create queue with low per-user limit
    _gate.clear()
    tq = TaskQueue(workers=2, max_queue_size=100)
    tq._max_pending_per_user = 3
    tq.start()

    # Submit 3 blocking tasks for user "alice" — should succeed
    h1 = tq.enqueue(_blocking, user_id="alice")
    h2 = tq.enqueue(_blocking, user_id="alice")
    h3 = tq.enqueue(_blocking, user_id="alice")
    ok("User alice: 3 tasks accepted", True)

    # 4th task for alice should be rejected (tasks still blocked)
    rejected = False
    try:
        tq.enqueue(_blocking, user_id="alice")
    except TaskUserLimitError:
        rejected = True
    ok("User alice: 4th task rejected", rejected)

    # Different user "bob" should still be able to submit
    try:
        h_bob = tq.enqueue(_blocking, user_id="bob")
        ok("User bob: task accepted", True)
    except TaskUserLimitError:
        ok("User bob: task accepted", False, "should not be rejected")

    # Release the gate — let all tasks complete
    _gate.set()
    h1.result(timeout=5)
    h2.result(timeout=5)
    h3.result(timeout=5)

    # After completion, alice should be able to submit again
    try:
        h4 = tq.enqueue(_noop, user_id="alice")
        ok("User alice: task after completion accepted", True)
        h4.result(timeout=5)
    except TaskUserLimitError:
        ok("User alice: task after completion accepted", False, "still rejected")

    # Verify pending count is 0
    ok(
        "Alice pending is 0",
        tq.get_user_pending("alice") == 0,
        f"got {tq.get_user_pending('alice')}",
    )

    # No user_id = unlimited (no limit check)
    for _ in range(10):
        tq.enqueue(_noop)
    ok("No user_id: unlimited submissions", True)

    # Zero limit = unlimited
    tq._max_pending_per_user = 0
    try:
        for _ in range(20):
            tq.enqueue(_noop, user_id="charlie")
        ok("Zero limit: unlimited submissions", True)
    except TaskUserLimitError:
        ok("Zero limit: unlimited submissions", False, "should not reject")

    tq.stop()

    # ── Circuit Breaker ─────────────────────────────────────
    print("\n--- Circuit Breaker ---")

    tq2 = TaskQueue(workers=2, max_queue_size=100)
    tq2._circuit_failure_threshold = 3
    tq2._circuit_recovery_timeout = 1.0  # 1s for fast test
    tq2._circuit_window = 60.0
    tq2.start()

    # Submit failing tasks — should open circuit after 3 failures
    handles = []
    for i in range(3):
        h = tq2.enqueue(_always_fail)
        handles.append(h)

    # Wait for all to fail
    for h in handles:
        with contextlib.suppress(RuntimeError):
            h.result(timeout=5)

    # Small delay for circuit breaker state to propagate
    time.sleep(0.1)

    # Circuit should be open now
    cb = tq2.get_circuit_breaker("_always_fail")
    ok("Circuit breaker created", cb is not None)
    if cb:
        ok("Circuit is OPEN", cb.state == CircuitState.OPEN, f"got {cb.state}")
        ok("Failure count is 3", cb.failure_count == 3, f"got {cb.failure_count}")

    # New submission should be rejected
    rejected = False
    try:
        tq2.enqueue(_always_fail)
    except TaskCircuitOpenError:
        rejected = True
    ok("Circuit OPEN rejects new tasks", rejected)

    # Different function should still work
    try:
        h_other = tq2.enqueue(_noop)
        h_other.result(timeout=5)
        ok("Different function still works", True)
    except TaskCircuitOpenError:
        ok("Different function still works", False, "should not be affected")

    # Wait for recovery timeout
    time.sleep(1.2)

    # Circuit should be HALF_OPEN now — one probe allowed
    try:
        h_probe = tq2.enqueue(_always_fail)
        ok("Half-open: probe allowed", True)
        # The probe will fail, re-opening the circuit
        with contextlib.suppress(RuntimeError):
            h_probe.result(timeout=5)
        time.sleep(0.1)
        cb2 = tq2.get_circuit_breaker("_always_fail")
        ok(
            "Half-open probe failed: re-opened",
            cb2 is not None and cb2.state == CircuitState.OPEN,
            f"got {cb2.state if cb2 else 'None'}",
        )
    except TaskCircuitOpenError:
        ok("Half-open: probe allowed", False, "should allow one probe")

    # Regression: the half-open gate must admit EXACTLY ONE probe. Drive
    # _check_circuit directly (deterministic, no timing) against a breaker whose
    # recovery window has already elapsed: the first call transitions
    # OPEN->HALF_OPEN and returns the single probe; every subsequent call must be
    # rejected until that probe's success/failure resolves. A prior off-by-one
    # left half_open_attempts=0 on the transition, so a SECOND probe slipped
    # through to the still-unhealthy dependency.
    with tq2._circuit_lock:
        tq2._circuit_breakers["_probe_gate"] = CircuitBreakerState(
            state=CircuitState.OPEN,
            failure_count=5,
            opened_at=time.monotonic() - 3600.0,  # window long since elapsed
        )
    first = tq2._check_circuit("_probe_gate")
    second = tq2._check_circuit("_probe_gate")
    third = tq2._check_circuit("_probe_gate")
    ok("Half-open: 1st probe admitted", first == "", f"got {first!r}")
    ok("Half-open: 2nd probe rejected", second != "", "double-probe off-by-one")
    ok("Half-open: 3rd probe rejected", third != "", f"got {third!r}")

    # Wait again and this time probe with a succeeding function
    time.sleep(1.2)

    # Manually set the breaker to test recovery with success
    with tq2._circuit_lock:
        cb3 = tq2._circuit_breakers.get("_always_fail")
        if cb3:
            # Replace with a function that succeeds to test recovery
            tq2._circuit_breakers["_noop_probe"] = CircuitBreakerState(
                state=CircuitState.OPEN,
                failure_count=3,
                opened_at=time.monotonic() - 2.0,  # expired timeout
            )

    try:
        h_success = tq2.enqueue(_noop)
        h_success.result(timeout=5)
        ok("Successful task records success", True)
    except TaskCircuitOpenError:
        ok("Successful task records success", False)

    # Test get_all_circuit_breakers
    all_breakers = tq2.get_all_circuit_breakers()
    ok("get_all_circuit_breakers returns dict", isinstance(all_breakers, dict))
    ok("Has tracked functions", len(all_breakers) > 0)

    tq2.stop()

    # ── Circuit breaker with successful recovery ─���──────────
    print("\n--- Circuit Breaker Recovery ---")

    tq3 = TaskQueue(workers=2, max_queue_size=100)
    tq3._circuit_failure_threshold = 2
    tq3._circuit_recovery_timeout = 0.5
    tq3._circuit_window = 60.0
    tq3.start()

    # Open the circuit
    for _ in range(2):
        h = tq3.enqueue(_always_fail)
        with contextlib.suppress(RuntimeError):
            h.result(timeout=5)
    time.sleep(0.1)

    cb = tq3.get_circuit_breaker("_always_fail")
    ok("Recovery: circuit opened", cb is not None and cb.state == CircuitState.OPEN)

    # Wait for recovery
    time.sleep(0.6)

    # Manually record a success to simulate successful probe
    tq3._record_circuit_success("_always_fail")
    # Check manually if it was half-open — it should transition on _check_circuit
    # Let's use the _check_circuit to transition to half-open first
    rejection = tq3._check_circuit("_always_fail")
    ok("Recovery: half-open transition", rejection == "", f"got rejection: {rejection}")

    # Now record success on the probe
    tq3._record_circuit_success("_always_fail")
    cb = tq3.get_circuit_breaker("_always_fail")
    ok(
        "Recovery: circuit closed after success",
        cb is not None and cb.state == CircuitState.CLOSED,
        f"got {cb.state if cb else 'None'}",
    )

    tq3.stop()

    # ── Summary ���─
    print(f"\n{'=' * 60}")
    total = PASS + FAIL
    print(f"Results: {PASS}/{total} passed, {FAIL} failed")
    if ERRORS:
        print("\nFailures:")
        for e in ERRORS:
            print(e)
    print("=" * 60)

    raise SystemExit(1 if FAIL > 0 else 0)


if __name__ == "__main__":
    main()
