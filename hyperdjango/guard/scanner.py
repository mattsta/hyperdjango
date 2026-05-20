"""
HyperGuard route scanner — startup validation and coverage reporting.

Scans a HyperApp's route table to detect:
- Guarded routes (have @guard decorator with _guard_spec)
- Unguarded routes (no guard — warns at startup)
- Guard requirement summaries for logging

Usage:
    from hyperdjango.guard.scanner import scan_routes, log_guard_summary

    results = scan_routes(app)
    log_guard_summary(results)
"""

from dataclasses import dataclass, field

from hyperdjango.guard.types import GuardSpec
from hyperdjango.logging import logger
from hyperdjango.router import Route, Router


@dataclass(frozen=True)
class GuardedRoute:
    """A route that has a @guard decorator."""

    method: str
    pattern: str
    handler_name: str
    requirement_names: tuple[str, ...]


@dataclass(frozen=True)
class UnguardedRoute:
    """A route without a @guard decorator."""

    method: str
    pattern: str
    handler_name: str


@dataclass
class ScanResult:
    """Results of scanning an app's route table for guard coverage."""

    guarded: list[GuardedRoute] = field(default_factory=list)
    unguarded: list[UnguardedRoute] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.guarded) + len(self.unguarded)

    @property
    def coverage_pct(self) -> float:
        if self.total == 0:
            return 100.0
        return len(self.guarded) / self.total * 100


# Routes that are expected to be unguarded (health checks, static, etc.)
_EXEMPT_PATTERNS = frozenset(
    {
        "/health",
        "/healthz",
        "/ready",
        "/readyz",
        "/metrics",
        "/static/{path:path}",
        "/favicon.ico",
    }
)


def scan_routes(app: object) -> ScanResult:
    """Scan an app's route table for guard coverage.

    Walks app.router._all_routes and checks each handler for _guard_spec
    attribute set by the @guard decorator.
    """
    result = ScanResult()
    router: Router = app.router
    routes: list[Route] = router._all_routes

    for route in routes:
        if route.pattern in _EXEMPT_PATTERNS:
            continue

        spec = _find_guard_spec(route.handler)
        name = route.handler.__qualname__

        if spec is not None:
            result.guarded.append(
                GuardedRoute(
                    method=route.method,
                    pattern=route.pattern,
                    handler_name=name,
                    requirement_names=spec.requirement_names,
                )
            )
        else:
            result.unguarded.append(
                UnguardedRoute(
                    method=route.method,
                    pattern=route.pattern,
                    handler_name=name,
                )
            )

    return result


def log_guard_summary(result: ScanResult) -> None:
    """Log guard coverage summary at startup."""
    logger.info(
        f"[GUARD] {len(result.guarded)} guarded routes, "
        f"{len(result.unguarded)} unguarded ({result.coverage_pct:.0f}% coverage)"
    )
    for route in result.unguarded:
        logger.warning(
            f"[GUARD] WARNING: {route.method} {route.pattern} "
            f"({route.handler_name}) has no guard"
        )


def _find_guard_spec(handler: object) -> GuardSpec | None:
    """Walk wrapper chain to find _guard_spec.

    @guard sets wrapper._guard_spec. functools.wraps sets __wrapped__.
    Follows __wrapped__ until the chain ends, using a seen-set for cycle detection.
    Route handlers are always Python functions so __dict__ is always available.
    """
    seen: set[int] = set()
    current = handler
    while id(current) not in seen:
        seen.add(id(current))
        fn_dict = current.__dict__
        spec = fn_dict.get("_guard_spec")
        if isinstance(spec, GuardSpec):
            return spec
        wrapped = fn_dict.get("__wrapped__")
        if wrapped is None:
            break
        current = wrapped
    return None
