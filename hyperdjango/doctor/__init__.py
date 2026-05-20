"""
HyperDjango Doctor — platform health diagnostic tool.

Usage:
    uv run hyper doctor              # Full diagnostic
    uv run hyper doctor --no-db      # Skip database checks
    uv run hyper doctor --json       # JSON output
    uv run hyper doctor --ci         # CI-friendly (exit code 1 on failure)
"""

from hyperdjango.doctor._registry import (
    CATEGORY_NAMES,
    CheckResult,
    CheckStatus,
    DoctorContext,
    DoctorReport,
    doctor_check,
)
from hyperdjango.doctor._runner import run_doctor

__all__ = [
    "CheckResult",
    "CheckStatus",
    "DoctorContext",
    "DoctorReport",
    "doctor_check",
    "run_doctor",
    "CATEGORY_NAMES",
]

# Import check modules to trigger decorator registration
import hyperdjango.doctor._checks_build  # noqa: F401
import hyperdjango.doctor._checks_config  # noqa: F401
import hyperdjango.doctor._checks_database  # noqa: F401
import hyperdjango.doctor._checks_filesystem  # noqa: F401
import hyperdjango.doctor._checks_perf  # noqa: F401
import hyperdjango.doctor._checks_python  # noqa: F401
import hyperdjango.doctor._checks_security  # noqa: F401
