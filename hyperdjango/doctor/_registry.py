"""Doctor check registry — dataclasses and decorator for registering checks."""

import enum
import threading
from dataclasses import dataclass


class CheckStatus(enum.Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"


@dataclass(slots=True)
class CheckResult:
    """Result of a single doctor check."""

    name: str
    category: str
    status: CheckStatus
    message: str
    detail: str = ""
    hint: str = ""
    duration_ns: int = 0
    metric_value: str = ""


@dataclass(slots=True)
class CategorySummary:
    """Aggregated results for one category."""

    name: str
    display_name: str
    checks: list[CheckResult]
    passed: int = 0
    warned: int = 0
    failed: int = 0
    skipped: int = 0
    duration_ns: int = 0


@dataclass(slots=True)
class DoctorReport:
    """Complete doctor run output."""

    categories: list[CategorySummary]
    total_passed: int = 0
    total_warned: int = 0
    total_failed: int = 0
    total_skipped: int = 0
    total_duration_ns: int = 0
    hyperdjango_version: str = ""
    python_version: str = ""
    timestamp: str = ""


@dataclass(slots=True)
class DoctorContext:
    """Shared state across doctor checks within a single run."""

    database_url: str = ""
    db_handle: int = -1
    verbose: bool = False
    skip_db: bool = False
    category_filter: str = ""


@dataclass(slots=True)
class RegisteredCheck:
    """A check function registered via @doctor_check."""

    func: object  # Callable[[DoctorContext], list[CheckResult]]
    name: str
    order: int = 100


# Category display names
CATEGORY_NAMES: dict[str, str] = {
    "build": "Build Health",
    "python": "Python Environment",
    "database": "Database Connectivity",
    "perf": "Performance Readiness",
    "config": "Configuration",
    "filesystem": "File System",
    "security": "Security",
}

# Category execution order
CATEGORY_ORDER: list[str] = [
    "build",
    "python",
    "database",
    "perf",
    "config",
    "filesystem",
    "security",
]

# Global registry: category → list of registered checks
_registry: dict[str, list[RegisteredCheck]] = {}
_doctor_registry_lock = threading.Lock()


def doctor_check(category: str, name: str, *, order: int = 100):
    """Register a doctor check function.

    Usage:
        @doctor_check("build", "native_extension")
        def check_native(ctx: DoctorContext) -> list[CheckResult]:
            ...
    """

    def decorator(func):
        with _doctor_registry_lock:
            _registry.setdefault(category, []).append(
                RegisteredCheck(func=func, name=name, order=order)
            )
        return func

    return decorator


def get_checks(category: str) -> list[RegisteredCheck]:
    """Get all registered checks for a category, sorted by order."""
    with _doctor_registry_lock:
        checks = _registry.get(category, [])
        return sorted(checks, key=lambda c: c.order)


def get_all_categories() -> list[str]:
    """Get all categories that have registered checks, in display order."""
    with _doctor_registry_lock:
        return [c for c in CATEGORY_ORDER if c in _registry]
