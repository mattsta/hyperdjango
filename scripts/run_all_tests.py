#!/usr/bin/env python3
"""
Unified test runner for HyperDjango.

Discovers and runs all scripts/test_*.py files, captures output,
parses pass/fail counts, and reports aggregate results.

Usage:
    uv run python scripts/run_all_tests.py              # Run all tests
    uv run python scripts/run_all_tests.py --filter orm  # Filter by pattern
    uv run python scripts/run_all_tests.py --parallel    # Run tests in parallel
    uv run python scripts/run_all_tests.py --verbose     # Show full output
    uv run python scripts/run_all_tests.py --fail-fast   # Stop on first failure
"""

import argparse
import re
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SuiteResult:
    """Result from running a single test file."""

    name: str
    passed: int = 0
    failed: int = 0
    duration: float = 0.0
    output: str = ""
    error: bool = False
    returncode: int = 0


def run_test_file(filepath: str, verbose: bool = False) -> SuiteResult:
    """Run a single test file and parse its results."""
    name = Path(filepath).name.replace("test_", "").replace(".py", "")
    start = time.monotonic()

    try:
        result = subprocess.run(
            [sys.executable, filepath],
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(Path(filepath).resolve().parent.parent),
        )
        duration = time.monotonic() - start
        output = result.stdout + result.stderr

        # Parse "Results: N/M passed, K failed" or "Results: N passed, K failed"
        passed, failed = parse_results(output)

        return SuiteResult(
            name=name,
            passed=passed,
            failed=failed,
            duration=duration,
            output=output,
            error=result.returncode != 0 and failed == 0,
            returncode=result.returncode,
        )
    except subprocess.TimeoutExpired:
        return SuiteResult(
            name=name,
            duration=time.monotonic() - start,
            error=True,
            output="TIMEOUT (>300s)",
            returncode=-1,
        )
    except Exception as e:
        return SuiteResult(
            name=name,
            duration=time.monotonic() - start,
            error=True,
            output=str(e),
            returncode=-1,
        )


def parse_results(output: str) -> tuple[int, int]:
    """Parse pass/fail counts from test output."""
    # Match: "Results: 59/59 passed, 0 failed"
    m = re.search(r"Results:\s*(\d+)/\d+\s*passed,\s*(\d+)\s*failed", output)
    if m:
        return int(m.group(1)), int(m.group(2))

    # Match: "Results: 59 passed, 0 failed"
    m = re.search(r"Results:\s*(\d+)\s*passed,\s*(\d+)\s*failed", output)
    if m:
        return int(m.group(1)), int(m.group(2))

    # Match: "N tests passed" style
    m = re.search(r"(\d+)\s*tests?\s*passed", output)
    if m:
        passed = int(m.group(1))
        m2 = re.search(r"(\d+)\s*failed", output)
        failed = int(m2.group(1)) if m2 else 0
        return passed, failed

    return 0, 0


def discover_tests(test_dir: str, pattern: str | None = None) -> list[str]:
    """Discover test files, optionally filtered by pattern."""
    files = sorted(str(p) for p in Path(test_dir).glob("test_*.py"))

    # Exclude this file and run_django_backend_tests.py
    exclude = {"run_all_tests.py", "run_django_backend_tests.py"}
    files = [f for f in files if Path(f).name not in exclude]

    if pattern:
        files = [f for f in files if pattern.lower() in Path(f).name.lower()]

    return files


def format_duration(seconds: float) -> str:
    """Format duration nicely."""
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    return f"{seconds:.1f}s"


def main():
    parser = argparse.ArgumentParser(description="HyperDjango unified test runner")
    parser.add_argument("--filter", "-f", help="Filter test files by pattern")
    parser.add_argument(
        "--parallel", "-p", action="store_true", help="Run tests in parallel"
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Show full output")
    parser.add_argument(
        "--fail-fast", action="store_true", help="Stop on first failure"
    )
    parser.add_argument(
        "--workers", "-w", type=int, default=4, help="Parallel workers (default: 4)"
    )
    args = parser.parse_args()

    test_dir = str(Path(__file__).resolve().parent)
    files = discover_tests(test_dir, args.filter)

    if not files:
        print("No test files found")
        return 1

    print(f"\n{'═' * 70}")
    print(f"  HyperDjango Test Runner — {len(files)} test suites")
    print(f"{'═' * 70}\n")

    total_start = time.monotonic()
    results: list[SuiteResult] = []

    if args.parallel:
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(run_test_file, f, args.verbose): f for f in files}
            for future in as_completed(futures):
                r = future.result()
                results.append(r)
                _print_suite_line(r)
                if args.fail_fast and (r.failed > 0 or r.error):
                    pool.shutdown(wait=False, cancel_futures=True)
                    break
    else:
        for filepath in files:
            r = run_test_file(filepath, args.verbose)
            results.append(r)
            _print_suite_line(r)

            if args.verbose and r.output:
                for line in r.output.splitlines():
                    print(f"    {line}")

            if args.fail_fast and (r.failed > 0 or r.error):
                break

    total_duration = time.monotonic() - total_start

    # Sort results by name for consistent output
    results.sort(key=lambda r: r.name)

    # Aggregate
    total_passed = sum(r.passed for r in results)
    total_failed = sum(r.failed for r in results)
    total_errors = sum(1 for r in results if r.error)
    suites_ok = sum(1 for r in results if r.failed == 0 and not r.error)

    # Print failures
    failed_suites = [r for r in results if r.failed > 0 or r.error]
    if failed_suites:
        print(f"\n{'─' * 70}")
        print("FAILURES:\n")
        for r in failed_suites:
            print(f"  {r.name}: {r.failed} failed" + (" (ERROR)" if r.error else ""))
            if r.output:
                # Show failure lines
                for line in r.output.splitlines():
                    if (
                        "FAIL" in line
                        or "✗" in line
                        or "Error" in line
                        or "Traceback" in line
                    ):
                        print(f"    {line}")

    # Summary
    print(f"\n{'═' * 70}")
    print(
        f"  TOTAL: {total_passed} passed, {total_failed} failed"
        + (f", {total_errors} errors" if total_errors else "")
    )
    print(f"  SUITES: {suites_ok}/{len(results)} passed")
    print(f"  TIME: {format_duration(total_duration)}")
    print(f"{'═' * 70}\n")

    return 0 if total_failed == 0 and total_errors == 0 else 1


def _print_suite_line(r: SuiteResult):
    """Print a single suite result line."""
    if r.error:
        status = "ERROR"
        icon = "!"
    elif r.failed > 0:
        status = f"{r.passed}/{r.passed + r.failed}"
        icon = "x"
    else:
        status = f"{r.passed}/{r.passed}"
        icon = "."

    dur = format_duration(r.duration)
    name = r.name.ljust(40)
    print(f"  [{icon}] {name} {status:>12}  ({dur})")


if __name__ == "__main__":
    sys.exit(main())
