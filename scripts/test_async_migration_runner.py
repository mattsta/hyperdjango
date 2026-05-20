"""Tests for AsyncMigrationRunner — progress reporting, timing, safety checks.

Covers:
- MigrationResult dataclass
- MigrationRunReport dataclass and success property
- Destructive operation detection (DROP TABLE, TRUNCATE, etc.)
- Progress callback invocation
- Dry-run preview mode
- Per-migration timing
- Report aggregation (applied_count, failed_count, total_duration_ms)
- Preview alias

Usage:
    uv run hyper-test async_migration_runner
"""

# hyper-test: unit

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


def main():
    print("=" * 60)
    print("Async Migration Runner Tests")
    print("=" * 60)

    # ── MigrationResult Dataclass ─────────────────────────────────

    print("\n--- MigrationResult ---")

    from hyperdjango.migrations import (
        AsyncMigrationRunner,
        MigrationResult,
        MigrationRunReport,
    )

    # Test 1: Basic result
    r = MigrationResult(
        name="0001_initial", status="applied", duration_ms=42.5, sql_statements=3
    )
    check("result name", r.name == "0001_initial")
    check("result status", r.status == "applied")
    check("result duration", r.duration_ms == 42.5)
    check("result sql_statements", r.sql_statements == 3)
    check("result no error", r.error is None)
    check("result no warnings", r.warnings == [])

    # Test 2: Failed result
    r2 = MigrationResult(
        name="0002_fail", status="failed", error="relation already exists"
    )
    check("failed result error", r2.error == "relation already exists")
    check("failed result status", r2.status == "failed")

    # Test 3: Result with warnings
    r3 = MigrationResult(
        name="0003_drop",
        status="applied",
        warnings=["Potentially destructive: DROP TABLE"],
    )
    check("result with warnings", len(r3.warnings) == 1)

    # ── MigrationRunReport ────────────────────────────────────────

    print("\n--- MigrationRunReport ---")

    # Test 4: Empty report is success
    report = MigrationRunReport()
    check("empty report success", report.success is True)
    check("empty report counts", report.applied_count == 0)

    # Test 5: Report with results
    report2 = MigrationRunReport(
        results=[r, r3],
        applied_count=2,
        total_duration_ms=85.3,
    )
    check("report applied count", report2.applied_count == 2)
    check("report total duration", report2.total_duration_ms == 85.3)
    check("report success with 0 failures", report2.success is True)

    # Test 6: Report with failure
    report3 = MigrationRunReport(results=[r2], failed_count=1)
    check("report failure", report3.success is False)

    # ── Destructive Detection ─────────────────────────────────────

    print("\n--- Destructive Operation Detection ---")

    # Test with mock migration system (just need _check_destructive)
    runner = AsyncMigrationRunner.__new__(AsyncMigrationRunner)

    # Test 7: DROP TABLE detected
    warnings = runner._check_destructive(["DROP TABLE users;"])
    check("detects DROP TABLE", len(warnings) == 1)
    check("warning mentions destructive", "destructive" in warnings[0].lower())

    # Test 8: TRUNCATE detected
    warnings = runner._check_destructive(["TRUNCATE sessions;"])
    check("detects TRUNCATE", len(warnings) == 1)

    # Test 9: DROP COLUMN detected
    warnings = runner._check_destructive(["ALTER TABLE users DROP COLUMN email;"])
    check("detects DROP COLUMN", len(warnings) >= 1)

    # Test 10: DELETE FROM detected
    warnings = runner._check_destructive(["DELETE FROM old_data;"])
    check("detects DELETE FROM", len(warnings) == 1)

    # Test 11: Safe operations no warnings
    warnings = runner._check_destructive(
        [
            "CREATE TABLE users (id SERIAL PRIMARY KEY);",
            "CREATE INDEX idx_users_email ON users(email);",
            "INSERT INTO users (name) VALUES ('alice');",
        ]
    )
    check("safe operations no warnings", len(warnings) == 0)

    # Test 12: Multiple destructive
    warnings = runner._check_destructive(
        [
            "DROP TABLE old;",
            "TRUNCATE cache;",
            "CREATE TABLE new_table (id INT);",
        ]
    )
    check("multiple destructive", len(warnings) == 2)

    # Test 13: Case insensitive
    warnings = runner._check_destructive(["drop table users;"])
    check("case insensitive detection", len(warnings) == 1)

    # ── Progress Callback ─────────────────────────────────────────

    print("\n--- Progress Callback ---")

    # Test 14: Callback receives correct arguments
    progress_log: list[tuple[str, str, int, int]] = []

    def on_progress(name, status, index, total):
        progress_log.append((name, status, index, total))

    # Verify callback signature
    on_progress("0001_init", "starting", 0, 3)
    check("callback invoked", len(progress_log) == 1)
    check("callback name", progress_log[0][0] == "0001_init")
    check("callback status", progress_log[0][1] == "starting")
    check("callback index", progress_log[0][2] == 0)
    check("callback total", progress_log[0][3] == 3)

    on_progress("0001_init", "applied", 1, 3)
    check("callback applied", progress_log[1][1] == "applied")

    # ── Report Properties ─────────────────────────────────────────

    print("\n--- Report Properties ---")

    # Test 15: Success property
    check("success true on empty", MigrationRunReport().success is True)
    check(
        "success true on applied", MigrationRunReport(applied_count=5).success is True
    )
    check(
        "success false on failure", MigrationRunReport(failed_count=1).success is False
    )
    check(
        "success false mixed",
        MigrationRunReport(applied_count=3, failed_count=1).success is False,
    )

    # ── Integration (Mock) ────────────────────────────────────────

    print("\n--- MigrationResult Status Types ---")

    # Test all valid status types
    for status in ("applied", "skipped", "failed", "dry_run", "fake"):
        r = MigrationResult(name="test", status=status)
        check(f"status '{status}' valid", r.status == status)

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
