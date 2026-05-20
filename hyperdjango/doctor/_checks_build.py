"""Doctor checks: Build Health."""

import importlib.machinery
import subprocess
import sys
import sysconfig
from pathlib import Path

from hyperdjango.doctor._registry import (
    CheckResult,
    CheckStatus,
    DoctorContext,
    doctor_check,
)


@doctor_check("build", "native_extension", order=10)
def check_native_extension(ctx: DoctorContext) -> list[CheckResult]:
    import hyperdjango._hyperdjango_native as _native

    so_path = Path(_native.__file__)
    size_mb = so_path.stat().st_size / (1024 * 1024)
    return [
        CheckResult(
            name="native_extension",
            category="build",
            status=CheckStatus.PASS,
            message="Native extension loaded",
            detail=f"{so_path.name} ({size_mb:.1f} MB)",
        )
    ]


@doctor_check("build", "build_mode", order=20)
def check_build_mode(ctx: DoctorContext) -> list[CheckResult]:
    from hyperdjango.native import is_release_build

    if is_release_build:
        return [
            CheckResult(
                name="build_mode",
                category="build",
                status=CheckStatus.PASS,
                message="Release build (ReleaseFast)",
            )
        ]
    return [
        CheckResult(
            name="build_mode",
            category="build",
            status=CheckStatus.WARN,
            message="Debug build detected",
            hint="Run: uv run hyper-build --install --release",
        )
    ]


@doctor_check("build", "abi_match", order=30)
def check_abi_match(ctx: DoctorContext) -> list[CheckResult]:
    import hyperdjango._hyperdjango_native as _native

    so_name = Path(_native.__file__).name
    expected_suffix = importlib.machinery.EXTENSION_SUFFIXES[0]
    soabi = sysconfig.get_config_var("SOABI") or ""

    if expected_suffix in so_name:
        return [
            CheckResult(
                name="abi_match",
                category="build",
                status=CheckStatus.PASS,
                message=f"ABI matches Python {sys.version_info.major}.{sys.version_info.minor}",
                detail=soabi,
            )
        ]
    return [
        CheckResult(
            name="abi_match",
            category="build",
            status=CheckStatus.FAIL,
            message=f"ABI mismatch: {so_name} vs expected {expected_suffix}",
            hint="Rebuild: uv run hyper-build",
        )
    ]


@doctor_check("build", "zig_version", order=40)
def check_zig_version(ctx: DoctorContext) -> list[CheckResult]:
    try:
        result = subprocess.run(
            ["zig", "version"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        version = result.stdout.strip()
        return [
            CheckResult(
                name="zig_version",
                category="build",
                status=CheckStatus.PASS,
                message=f"Zig {version}",
            )
        ]
    except FileNotFoundError, subprocess.TimeoutExpired:
        return [
            CheckResult(
                name="zig_version",
                category="build",
                status=CheckStatus.SKIP,
                message="Zig compiler not found in PATH",
            )
        ]
