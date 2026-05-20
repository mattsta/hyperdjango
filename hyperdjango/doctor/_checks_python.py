"""Doctor checks: Python Environment."""

import importlib.util
import sys

from hyperdjango.doctor._registry import (
    CheckResult,
    CheckStatus,
    DoctorContext,
    doctor_check,
)


@doctor_check("python", "python_version", order=10)
def check_python_version(ctx: DoctorContext) -> list[CheckResult]:
    v = sys.version_info
    version_str = f"{v.major}.{v.minor}.{v.micro}"
    if v >= (3, 14):
        return [
            CheckResult(
                name="python_version",
                category="python",
                status=CheckStatus.PASS,
                message=f"Python {version_str}",
            )
        ]
    return [
        CheckResult(
            name="python_version",
            category="python",
            status=CheckStatus.FAIL,
            message=f"Python {version_str} — requires 3.14+",
            hint="Install Python 3.14+ via pyenv: pyenv install 3.14.0",
        )
    ]


@doctor_check("python", "free_threaded", order=20)
def check_free_threaded(ctx: DoctorContext) -> list[CheckResult]:
    # dynamic-attr: sys._is_gil_enabled exists only on free-threaded CPython builds; fall back to a GIL-enabled assumption on standard builds
    gil_enabled = getattr(sys, "_is_gil_enabled", lambda: True)()
    if not gil_enabled:
        return [
            CheckResult(
                name="free_threaded",
                category="python",
                status=CheckStatus.PASS,
                message="Free-threaded (GIL disabled)",
            )
        ]
    return [
        CheckResult(
            name="free_threaded",
            category="python",
            status=CheckStatus.WARN,
            message="GIL is enabled — reduced concurrency",
            hint="Use Python 3.14t free-threaded build",
        )
    ]


@doctor_check("python", "required_packages", order=30)
def check_required_packages(ctx: DoctorContext) -> list[CheckResult]:
    required = ["argon2"]
    missing: list[str] = []
    for pkg in required:
        if importlib.util.find_spec(pkg) is None:
            missing.append(pkg)

    if not missing:
        return [
            CheckResult(
                name="required_packages",
                category="python",
                status=CheckStatus.PASS,
                message=f"Required packages: {', '.join(required)}",
            )
        ]
    return [
        CheckResult(
            name="required_packages",
            category="python",
            status=CheckStatus.FAIL,
            message=f"Missing: {', '.join(missing)}",
            hint=f"Install: uv add {' '.join(missing)}",
        )
    ]


@doctor_check("python", "optional_packages", order=40)
def check_optional_packages(ctx: DoctorContext) -> list[CheckResult]:
    optional = {"django": "Django ORM bridge", "jinja2": "Jinja2 templates"}
    found: list[str] = []
    for pkg, desc in optional.items():
        if importlib.util.find_spec(pkg) is not None:
            found.append(pkg)

    return [
        CheckResult(
            name="optional_packages",
            category="python",
            status=CheckStatus.PASS,
            message=f"Optional: {', '.join(found)}"
            if found
            else "No optional packages",
        )
    ]
