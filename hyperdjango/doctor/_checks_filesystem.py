"""Doctor checks: File System."""

import os
from pathlib import Path

from hyperdjango.doctor._registry import (
    CheckResult,
    CheckStatus,
    DoctorContext,
    doctor_check,
)


def _project_root() -> Path:
    """Best-effort project root detection."""
    cwd = Path.cwd()
    if (cwd / "pyproject.toml").exists():
        return cwd
    if (cwd / "hyperdjango").is_dir():
        return cwd
    return cwd


@doctor_check("filesystem", "template_dir", order=10)
def check_template_dir(ctx: DoctorContext) -> list[CheckResult]:
    root = _project_root()
    template_dir = root / "templates"
    if template_dir.is_dir():
        count = sum(1 for _ in template_dir.rglob("*.html"))
        return [
            CheckResult(
                name="template_dir",
                category="filesystem",
                status=CheckStatus.PASS,
                message=f"templates/ found ({count} .html files)",
            )
        ]
    return [
        CheckResult(
            name="template_dir",
            category="filesystem",
            status=CheckStatus.SKIP,
            message="No templates/ directory",
        )
    ]


@doctor_check("filesystem", "static_dir", order=20)
def check_static_dir(ctx: DoctorContext) -> list[CheckResult]:
    root = _project_root()
    static_dir = root / "static"
    if static_dir.is_dir():
        return [
            CheckResult(
                name="static_dir",
                category="filesystem",
                status=CheckStatus.PASS,
                message="static/ directory found",
            )
        ]
    return [
        CheckResult(
            name="static_dir",
            category="filesystem",
            status=CheckStatus.SKIP,
            message="No static/ directory",
        )
    ]


@doctor_check("filesystem", "bytecode_cache", order=30)
def check_bytecode_cache(ctx: DoctorContext) -> list[CheckResult]:
    root = _project_root()
    cache_dir = root / "templates" / "__pycache__" / "hztc"
    if cache_dir.exists():
        writable = os.access(cache_dir, os.W_OK)
        count = sum(1 for _ in cache_dir.glob("*.hztc"))
        if writable:
            return [
                CheckResult(
                    name="bytecode_cache",
                    category="filesystem",
                    status=CheckStatus.PASS,
                    message=f"Bytecode cache writable ({count} cached templates)",
                )
            ]
        return [
            CheckResult(
                name="bytecode_cache",
                category="filesystem",
                status=CheckStatus.WARN,
                message="Bytecode cache not writable",
                hint=f"Fix permissions: chmod 755 {cache_dir}",
            )
        ]
    return [
        CheckResult(
            name="bytecode_cache",
            category="filesystem",
            status=CheckStatus.SKIP,
            message="No bytecode cache directory (created on first render)",
        )
    ]


@doctor_check("filesystem", "migrations_dir", order=40)
def check_migrations_dir(ctx: DoctorContext) -> list[CheckResult]:
    root = _project_root()
    mig_dir = root / "migrations"
    if mig_dir.is_dir():
        count = sum(1 for f in mig_dir.glob("*.py") if f.name.startswith("0"))
        return [
            CheckResult(
                name="migrations_dir",
                category="filesystem",
                status=CheckStatus.PASS,
                message=f"migrations/ found ({count} migration files)",
            )
        ]
    return [
        CheckResult(
            name="migrations_dir",
            category="filesystem",
            status=CheckStatus.SKIP,
            message="No migrations/ directory",
        )
    ]
