"""
System check framework — validate configuration at startup.

Inspired by Django's system check framework. Registers checks that run
before the server starts, catching configuration errors early.

Usage:
    from hyperdjango.checks import register, CheckMessage, run_checks

    @register("security")
    def check_secret_key(app):
        if not app.secret_key:
            return [CheckMessage(
                level="error",
                msg="SECRET_KEY is not configured",
                hint="Set SECRET_KEY environment variable or pass to HyperApp()",
                id="security.E001",
            )]
        return []

    # Run all checks
    messages = run_checks(app)
    for msg in messages:
        print(f"[{msg.level}] {msg.id}: {msg.msg}")
"""

import contextlib
import threading
from collections.abc import Callable
from dataclasses import dataclass

from hyperdjango.conf import get_setting, parse_bool

_SERIOUS_LEVELS = frozenset({"error", "critical"})


@dataclass(slots=True)
class CheckMessage:
    """A single check result message.

    Attributes:
        level: "info", "warning", "error", or "critical"
        msg: Human-readable description of the issue
        hint: Suggestion for fixing the issue
        id: Unique identifier (e.g., "security.E001")
        obj: The object that has the issue (optional)
    """

    level: str
    msg: str
    hint: str = ""
    id: str = ""
    obj: object = None

    def __str__(self) -> str:
        prefix = f"({self.id}) " if self.id else ""
        hint_str = f"\n\tHINT: {self.hint}" if self.hint else ""
        return f"{prefix}{self.msg}{hint_str}"

    @property
    def is_serious(self) -> bool:
        """True if this is an error or critical issue."""
        return self.level in _SERIOUS_LEVELS


# ── Check registry ─────────────────────────────────────────────────────────

_registry: dict[str, list[Callable]] = {}
_registry_lock = threading.Lock()


def register(tag: str = ""):
    """Register a check function.

    The check function receives the app instance and returns a list
    of CheckMessage objects (empty list = no issues).

    Args:
        tag: Category tag for grouping checks (e.g., "security", "database", "models").

    Usage:
        @register("security")
        def check_secret_key(app):
            ...
            return []

        @register()  # no tag = runs always
        def check_something(app):
            ...
    """

    def decorator(func: Callable) -> Callable:
        with _registry_lock:
            _registry.setdefault(tag, []).append(func)
        return func

    return decorator


def run_checks(
    app, tags: list[str] | None = None, include_deployment: bool = False
) -> list[CheckMessage]:
    """Run all registered checks and return messages.

    Args:
        app: HyperApp instance to check.
        tags: Only run checks with these tags. None = all.
        include_deployment: Include deployment-specific checks.

    Returns:
        List of CheckMessage objects sorted by severity.
    """
    messages: list[CheckMessage] = []
    level_order = {"critical": 0, "error": 1, "warning": 2, "info": 3}

    with _registry_lock:
        registry_snapshot = list(_registry.items())
    for tag, checks in registry_snapshot:
        if tags and tag not in tags and tag != "":
            continue
        if tag == "deployment" and not include_deployment:
            continue
        for check_fn in checks:
            try:
                result = check_fn(app)
                if result:
                    messages.extend(result)
            # blind-except: system-check runner records a raising check as an error CheckMessage so one bad check does not abort the whole run
            except Exception as e:
                messages.append(
                    CheckMessage(
                        level="error",
                        msg=f"Check {check_fn.__name__} raised: {e}",
                        id="checks.E999",
                    )
                )

    messages.sort(key=lambda m: level_order.get(m.level, 4))
    return messages


def get_check_count(messages: list[CheckMessage]) -> dict[str, int]:
    """Count messages by level."""
    counts: dict[str, int] = {"critical": 0, "error": 0, "warning": 0, "info": 0}
    for msg in messages:
        counts[msg.level] = counts.get(msg.level, 0) + 1
    return counts


# ── Built-in checks ────────────────────────────────────────────────────────


@register("security")
def check_secret_key(app) -> list[CheckMessage]:
    """Check that a secret key is configured for production."""
    messages: list[CheckMessage] = []
    # Resolve through get_setting so Django (HYPERDJANGO_SECRET_KEY) and the
    # HYPER_SECRET_KEY env var are honored — a bare SECRET_KEY env var is never
    # where the framework looks for the key.
    secret = str(get_setting("SECRET_KEY", "") or "")
    if not secret:
        messages.append(
            CheckMessage(
                level="warning",
                msg="SECRET_KEY environment variable is not set",
                hint="Set SECRET_KEY to a random string of 50+ characters for production",
                id="security.W001",
            )
        )
    elif len(secret) < 50:
        messages.append(
            CheckMessage(
                level="warning",
                msg=f"SECRET_KEY is only {len(secret)} characters (recommended: 50+)",
                hint='Use a longer secret key: python -c "import secrets; print(secrets.token_hex(32))"',
                id="security.W002",
            )
        )
    return messages


@register("security")
def check_security_middleware(app) -> list[CheckMessage]:
    """Check that security middleware is configured — inspecting chain contents."""
    messages: list[CheckMessage] = []
    stack = app.__dict__.get("_middleware")
    chain = list(stack._middleware or []) if stack is not None else []
    if not chain:
        messages.append(
            CheckMessage(
                level="warning",
                msg="No middleware configured",
                hint="Add SecurityHeadersMiddleware and CSRFMiddleware for production",
                id="security.W003",
            )
        )
        return messages

    # Identify chain members by their class/function name so we can verify the
    # security-critical middleware are actually present, not merely that *some*
    # middleware exists.
    names = set()
    for mw in chain:
        names.add(type(mw).__name__)
        # Chain members are heterogeneous — class instances OR plain functions.
        # A function/class carries its own __name__; an instance usually doesn't.
        # dynamic-attr: __name__ is genuinely optional across this mixed function/instance collection
        fn_name = getattr(mw, "__name__", None)
        if isinstance(fn_name, str):
            names.add(fn_name)
    if not any("SecurityHeaders" in n for n in names):
        messages.append(
            CheckMessage(
                level="warning",
                msg="SecurityHeadersMiddleware is not in the middleware chain",
                hint="Add SecurityHeadersMiddleware to emit HSTS/nosniff/frame-options headers",
                id="security.W004",
            )
        )
    if not any("CSRF" in n.upper() for n in names):
        messages.append(
            CheckMessage(
                level="warning",
                msg="CSRFMiddleware is not in the middleware chain",
                hint="Add CSRFMiddleware to protect state-changing requests from CSRF",
                id="security.W005",
            )
        )
    return messages


@register("database")
def check_database_configured(app) -> list[CheckMessage]:
    """Check that a database is configured."""
    messages: list[CheckMessage] = []
    db = None
    with contextlib.suppress(AttributeError, RuntimeError):
        db = app._db
    if db is None:
        messages.append(
            CheckMessage(
                level="info",
                msg="No database configured",
                hint="Pass database= to HyperApp() for database features",
                id="database.I001",
            )
        )
    return messages


@register("deployment")
def check_debug_off(app) -> list[CheckMessage]:
    """Check that debug mode is off in deployment."""
    messages: list[CheckMessage] = []
    # parse_bool so every truthy form ("1", "true", "yes", "on", "TRUE", …)
    # trips the check, not only the literal "1"; resolve via get_setting so a
    # Django HYPERDJANGO_DEBUG override is seen too.
    if parse_bool(get_setting("DEBUG", False)):
        messages.append(
            CheckMessage(
                level="error",
                msg="DEBUG is enabled in production deployment",
                hint="Unset HYPER_DEBUG / HYPERDJANGO_DEBUG (or set it to a falsy value) for production",
                id="deployment.E001",
            )
        )
    return messages


@register("deployment")
def check_allowed_hosts(app) -> list[CheckMessage]:
    """Check that allowed hosts is configured for deployment."""
    messages: list[CheckMessage] = []
    # Resolve through get_setting so HYPER_ALLOWED_HOSTS / Django settings are
    # honored — a bare ALLOWED_HOSTS env var is never where the framework looks.
    # Fall back to the app's resolved value so a
    # programmatically-configured deploy (HyperApp(allowed_hosts=...)) is seen.
    hosts = get_setting("ALLOWED_HOSTS", None)
    if not hosts:
        with contextlib.suppress(AttributeError):
            hosts = app.allowed_hosts
    if not hosts:
        messages.append(
            CheckMessage(
                level="warning",
                msg="ALLOWED_HOSTS not configured",
                hint="Set ALLOWED_HOSTS=yourdomain.com for production",
                id="deployment.W001",
            )
        )
    return messages
