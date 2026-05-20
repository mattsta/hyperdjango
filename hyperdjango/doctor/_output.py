"""Doctor output formatters — terminal (color), JSON, CI."""

import json as _stdlib_json
import sys

from hyperdjango.doctor._registry import (
    CheckStatus,
    DoctorReport,
)


def _write(s: str) -> None:
    sys.stdout.write(s)


# ANSI color codes
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RESET = "\033[0m"

_STATUS_SYMBOLS = {
    CheckStatus.PASS: f"{_GREEN}PASS{_RESET}",
    CheckStatus.WARN: f"{_YELLOW}WARN{_RESET}",
    CheckStatus.FAIL: f"{_RED}FAIL{_RESET}",
    CheckStatus.SKIP: f"{_DIM}SKIP{_RESET}",
}

_STATUS_PLAIN = {
    CheckStatus.PASS: "PASS",
    CheckStatus.WARN: "WARN",
    CheckStatus.FAIL: "FAIL",
    CheckStatus.SKIP: "SKIP",
}


def render_terminal(report: DoctorReport) -> None:
    """Render report with ANSI colors to stdout."""
    _write(f"\n  {_BOLD}HyperDjango Doctor v{report.hyperdjango_version}{_RESET}\n")
    _write(f"  {'=' * 50}\n\n")

    for cat in report.categories:
        _write(f"  {_BOLD}{cat.display_name}{_RESET}\n")
        for check in cat.checks:
            symbol = _STATUS_SYMBOLS[check.status]
            line = f"    {symbol}  {check.message:<42}"
            if check.detail:
                line += f"  {_DIM}{check.detail}{_RESET}"
            if check.metric_value:
                line += f"  {_DIM}{check.metric_value}{_RESET}"
            _write(f"{line}\n")
            if check.hint and check.status in (CheckStatus.WARN, CheckStatus.FAIL):
                _write(f"          {_DIM}{check.hint}{_RESET}\n")
        _write("\n")

    # Summary
    total = (
        report.total_passed
        + report.total_warned
        + report.total_failed
        + report.total_skipped
    )
    elapsed_ms = report.total_duration_ns / 1_000_000
    _write(f"  {'=' * 50}\n")
    parts: list[str] = []
    if report.total_passed:
        parts.append(f"{_GREEN}{report.total_passed} passed{_RESET}")
    if report.total_warned:
        parts.append(f"{_YELLOW}{report.total_warned} warnings{_RESET}")
    if report.total_failed:
        parts.append(f"{_RED}{report.total_failed} failed{_RESET}")
    if report.total_skipped:
        parts.append(f"{_DIM}{report.total_skipped} skipped{_RESET}")
    _write(f"  {total} checks: {', '.join(parts)} ({elapsed_ms:.0f}ms)\n")
    _write("\n")


def render_json(report: DoctorReport) -> None:
    """Render report as JSON to stdout."""
    data = {
        "version": report.hyperdjango_version,
        "python": report.python_version,
        "timestamp": report.timestamp,
        "duration_ms": report.total_duration_ns / 1_000_000,
        "summary": {
            "passed": report.total_passed,
            "warned": report.total_warned,
            "failed": report.total_failed,
            "skipped": report.total_skipped,
        },
        "categories": [
            {
                "name": cat.name,
                "display_name": cat.display_name,
                "checks": [
                    {
                        "name": c.name,
                        "status": c.status.value,
                        "message": c.message,
                        "detail": c.detail,
                        "hint": c.hint,
                        "metric_value": c.metric_value,
                        "duration_ms": c.duration_ns / 1_000_000,
                    }
                    for c in cat.checks
                ],
            }
            for cat in report.categories
        ],
    }
    _write(f"{_stdlib_json.dumps(data, indent=2)}\n")


def render_ci(report: DoctorReport) -> None:
    """Render compact CI-friendly output (no color)."""
    for cat in report.categories:
        for check in cat.checks:
            status = _STATUS_PLAIN[check.status]
            _write(f"{status}  {cat.name}/{check.name}: {check.message}\n")

    total = (
        report.total_passed
        + report.total_warned
        + report.total_failed
        + report.total_skipped
    )
    elapsed_ms = report.total_duration_ns / 1_000_000
    _write(
        f"\n{total} checks: {report.total_passed} passed, {report.total_warned} warned, {report.total_failed} failed, {report.total_skipped} skipped ({elapsed_ms:.0f}ms)\n"
    )
