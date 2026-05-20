"""Tests for connection pool auto-tuner.
# hyper-test: unit

Covers:
- PoolAutoTuner initialization and configuration
- Utilization calculation and thresholds
- Scale-up recommendations (high utilization, zero available, thread pressure)
- Scale-down recommendations (low utilization with cooldown hysteresis)
- Stats tracking and history
- Recommendation engine (recent sample aggregation)
- Saturation detection
- Start/stop lifecycle

Usage:
    uv run hyper-test pool_autotuner
"""

import sys

RESULTS = {"passed": 0, "failed": 0, "errors": []}


def check(name, condition, details=""):
    if condition:
        RESULTS["passed"] += 1
        print(f"  PASS: {name}")
    else:
        RESULTS["failed"] += 1
        RESULTS["errors"].append(name)
        print(f"  FAIL: {name} — {details}")


class MockDatabase:
    """Mock database with configurable pool stats."""

    def __init__(self, total=10, available=5, in_use=5, thread_owned=2):
        self._stats = {
            "total": total,
            "available": available,
            "in_use": in_use,
            "missing": 0,
            "thread_owned": thread_owned,
        }

    def pool_stats(self):
        return dict(self._stats)

    def set_stats(self, **kwargs):
        self._stats.update(kwargs)


def main():
    import asyncio

    return asyncio.run(run_tests())


async def run_tests():
    from hyperdjango.pool import PoolAutoTuner

    print("=" * 60)
    print("Pool Auto-Tuner Tests")
    print("=" * 60)

    # ── Initialization ────────────────────────────────────────────

    print("\n--- Initialization ---")

    db = MockDatabase()
    tuner = PoolAutoTuner(db, check_interval=1)
    check("default check interval", tuner._check_interval == 1)
    check("default scale_up_threshold", tuner._scale_up_threshold == 0.8)
    check("default scale_down_threshold", tuner._scale_down_threshold == 0.3)
    check("default cooldown_periods", tuner._cooldown_periods == 6)
    check("not running initially", not tuner._running)
    check("no samples initially", len(tuner._samples) == 0)

    # Custom config
    tuner2 = PoolAutoTuner(
        db,
        check_interval=5,
        scale_up_threshold=0.9,
        scale_down_threshold=0.2,
        cooldown_periods=3,
        scale_step=4,
    )
    check("custom scale_up_threshold", tuner2._scale_up_threshold == 0.9)
    check("custom cooldown_periods", tuner2._cooldown_periods == 3)

    # ── Scale-Up Detection ────────────────────────────────────────

    print("\n--- Scale-Up Detection ---")

    # High utilization (> 80%)
    db_high = MockDatabase(total=10, available=1, in_use=9, thread_owned=5)
    tuner_up = PoolAutoTuner(db_high, check_interval=1)
    await tuner_up._check_and_adjust()
    check("high util detected", tuner_up._samples[-1]["action"] == "scale_up")
    check("utilization recorded", tuner_up._samples[-1]["utilization"] == 0.9)
    check("scale_up_count incremented", tuner_up._scale_up_count == 1)

    # Zero available connections
    db_zero = MockDatabase(total=10, available=0, in_use=10, thread_owned=8)
    tuner_zero = PoolAutoTuner(db_zero, check_interval=1)
    await tuner_zero._check_and_adjust()
    check(
        "zero available triggers scale_up",
        tuner_zero._samples[-1]["action"] == "scale_up",
    )

    # Thread pressure (> 75% of 64 slots)
    db_thread = MockDatabase(total=10, available=3, in_use=7, thread_owned=50)
    tuner_thread = PoolAutoTuner(db_thread, check_interval=1)
    await tuner_thread._check_and_adjust()
    check(
        "thread pressure triggers scale_up",
        tuner_thread._samples[-1]["action"] == "scale_up",
    )
    check(
        "thread_pressure recorded", tuner_thread._samples[-1]["thread_pressure"] > 0.75
    )

    # ── Scale-Down Detection ──────────────────────────────────────

    print("\n--- Scale-Down Detection ---")

    # Low utilization — needs consecutive checks
    db_low = MockDatabase(total=10, available=8, in_use=2, thread_owned=1)
    tuner_down = PoolAutoTuner(db_low, check_interval=1, cooldown_periods=3)

    # First 2 checks — should hold (not enough consecutive low)
    await tuner_down._check_and_adjust()
    check("first low check holds", tuner_down._samples[-1]["action"] == "hold")
    check("consecutive_low=1", tuner_down._consecutive_low == 1)

    await tuner_down._check_and_adjust()
    check("second low check holds", tuner_down._samples[-1]["action"] == "hold")
    check("consecutive_low=2", tuner_down._consecutive_low == 2)

    # Third check — triggers scale_down
    await tuner_down._check_and_adjust()
    check(
        "third low check triggers scale_down",
        tuner_down._samples[-1]["action"] == "scale_down",
    )
    check("scale_down_count=1", tuner_down._scale_down_count == 1)
    check("consecutive_low reset", tuner_down._consecutive_low == 0)

    # ── Hysteresis (interruption resets consecutive low) ──────────

    print("\n--- Hysteresis ---")

    db_mixed = MockDatabase(total=10, available=8, in_use=2, thread_owned=1)
    tuner_hyst = PoolAutoTuner(db_mixed, check_interval=1, cooldown_periods=3)

    await tuner_hyst._check_and_adjust()
    check("hyst low check 1", tuner_hyst._consecutive_low == 1)

    # Spike in usage interrupts the cooldown
    db_mixed.set_stats(available=2, in_use=8)
    await tuner_hyst._check_and_adjust()
    check("hyst spike resets consecutive", tuner_hyst._consecutive_low == 0)

    # Back to low — must restart countdown
    db_mixed.set_stats(available=8, in_use=2)
    await tuner_hyst._check_and_adjust()
    check("hyst restart after spike", tuner_hyst._consecutive_low == 1)

    # ── Stats ─────────────────────────────────────────────────────

    print("\n--- Stats ---")

    stats = tuner_up.stats()
    check("stats has total_samples", stats["total_samples"] == 1)
    check("stats has scale_up_recommendations", stats["scale_up_recommendations"] == 1)
    check("stats has recent_samples", len(stats["recent_samples"]) > 0)
    check("stats has running", stats["running"] is False)

    # ── Recommendation Engine ─────────────────────────────────────

    print("\n--- Recommendation Engine ---")

    # Feed 3 scale_up samples
    db_rec = MockDatabase(total=10, available=0, in_use=10, thread_owned=5)
    tuner_rec = PoolAutoTuner(db_rec, check_interval=1)
    for _ in range(3):
        await tuner_rec._check_and_adjust()
    check("recommendation scale_up", tuner_rec.recommendation() == "scale_up")

    # Feed 3 scale_down samples (needs cooldown)
    db_rec2 = MockDatabase(total=10, available=8, in_use=2, thread_owned=1)
    tuner_rec2 = PoolAutoTuner(db_rec2, check_interval=1, cooldown_periods=1)
    for _ in range(3):
        await tuner_rec2._check_and_adjust()
    check("recommendation scale_down", tuner_rec2.recommendation() == "scale_down")

    # Insufficient data
    tuner_empty = PoolAutoTuner(MockDatabase(), check_interval=1)
    check(
        "recommendation insufficient_data",
        tuner_empty.recommendation() == "insufficient_data",
    )

    # ── Saturation Detection ──────────────────────────────────────

    print("\n--- Saturation Detection ---")

    db_sat = MockDatabase(total=10, available=0, in_use=10, thread_owned=8)
    tuner_sat = PoolAutoTuner(db_sat, check_interval=1)
    await tuner_sat._check_and_adjust()
    check("is_saturated when zero available", tuner_sat.is_saturated is True)

    db_ok = MockDatabase(total=10, available=5, in_use=5, thread_owned=3)
    tuner_ok = PoolAutoTuner(db_ok, check_interval=1)
    await tuner_ok._check_and_adjust()
    check("not saturated when healthy", tuner_ok.is_saturated is False)

    # Empty tuner
    tuner_no_data = PoolAutoTuner(MockDatabase(), check_interval=1)
    check("not saturated with no data", tuner_no_data.is_saturated is False)

    # ── Utilization History ───────────────────────────────────────

    print("\n--- Utilization History ---")

    db_hist = MockDatabase(total=10)
    tuner_hist = PoolAutoTuner(db_hist, check_interval=1)

    utilizations = [0.3, 0.5, 0.7, 0.9, 0.6]
    for u in utilizations:
        in_use = int(u * 10)
        db_hist.set_stats(available=10 - in_use, in_use=in_use)
        await tuner_hist._check_and_adjust()

    history = tuner_hist.utilization_history
    check("utilization history length", len(history) == 5)
    check("utilization history values", all(0 <= h <= 1 for h in history))

    # ── Sample Retention ──────────────────────────────────────────

    print("\n--- Sample Retention ---")

    db_ret = MockDatabase(total=10, available=5, in_use=5, thread_owned=2)
    tuner_ret = PoolAutoTuner(db_ret, check_interval=1)
    tuner_ret._max_samples = 10  # Small for testing

    for _ in range(20):
        await tuner_ret._check_and_adjust()
    check("sample retention limit", len(tuner_ret._samples) == 10)

    # ── Start/Stop Lifecycle ──────────────────────────────────────

    print("\n--- Start/Stop Lifecycle ---")

    db_life = MockDatabase(total=10, available=5, in_use=5, thread_owned=2)
    tuner_life = PoolAutoTuner(
        db_life, check_interval=60
    )  # Long interval so it doesn't actually fire
    tuner_life.start()
    check("started", tuner_life._running is True)
    check("task created", tuner_life._task is not None)

    tuner_life.stop()
    check("stopped", tuner_life._running is False)

    # Double start
    tuner_life.start()
    tuner_life.start()  # Should be idempotent
    check("double start idempotent", tuner_life._running is True)
    tuner_life.stop()

    # ── Empty Pool (total=0) ──────────────────────────────────────

    print("\n--- Edge Cases ---")

    db_empty = MockDatabase(total=0, available=0, in_use=0, thread_owned=0)
    tuner_edge = PoolAutoTuner(db_empty, check_interval=1)
    await tuner_edge._check_and_adjust()
    check("empty pool no crash", len(tuner_edge._samples) == 0)

    # ── Summary ──────────────────────────────────────────────────

    print("\n" + "=" * 60)
    total = RESULTS["passed"] + RESULTS["failed"]
    print(f"Results: {RESULTS['passed']}/{total} passed")
    if RESULTS["errors"]:
        print(f"Failures: {', '.join(RESULTS['errors'])}")
    print("=" * 60)

    return RESULTS["failed"] == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
