"""Doctor checks: Security posture."""

import contextlib

from hyperdjango.doctor._registry import (
    CheckResult,
    CheckStatus,
    DoctorContext,
    doctor_check,
)


@doctor_check("security", "argon2_available", order=10)
def check_argon2(ctx: DoctorContext) -> list[CheckResult]:
    import argon2

    hasher = argon2.PasswordHasher()
    test_hash = hasher.hash("doctor_test")
    verified = hasher.verify(test_hash, "doctor_test")

    return [
        CheckResult(
            name="argon2_available",
            category="security",
            status=CheckStatus.PASS if verified else CheckStatus.FAIL,
            message="Argon2 password hashing ready",
        )
    ]


@doctor_check("security", "secret_key", order=20)
def check_secret_key(ctx: DoctorContext) -> list[CheckResult]:
    from hyperdjango.conf import get_setting

    # The framework reads HYPER_SECRET_KEY (env) / HYPERDJANGO_SECRET_KEY
    # (Django) — NOT a bare SECRET_KEY. Resolve the effective value the same
    # way the app does so the check reflects what actually ships.
    secret = str(get_setting("SECRET_KEY", "") or "")
    if not secret:
        return [
            CheckResult(
                name="secret_key",
                category="security",
                status=CheckStatus.WARN,
                message="SECRET_KEY not set",
                hint="Set HYPER_SECRET_KEY env var (or HYPERDJANGO_SECRET_KEY) for production",
            )
        ]
    if len(secret) < 32:
        return [
            CheckResult(
                name="secret_key",
                category="security",
                status=CheckStatus.WARN,
                message=f"SECRET_KEY too short ({len(secret)} chars)",
                hint="Use at least 32 random characters",
            )
        ]
    return [
        CheckResult(
            name="secret_key",
            category="security",
            status=CheckStatus.PASS,
            message=f"SECRET_KEY configured ({len(secret)} chars)",
        )
    ]


@doctor_check("security", "csrf_secret", order=30)
def check_csrf_secret(ctx: DoctorContext) -> list[CheckResult]:
    # CSRF_SECRET has a per-process random default, so get_setting() always
    # returns something non-empty. To detect whether it was EXPLICITLY set for
    # production we resolve the effective value the way the app does (which sees
    # BOTH HYPER_CSRF_SECRET env AND the HYPERDJANGO_CSRF_SECRET Django override
    # — the latter was ignored when this only read os.environ) and treat a value
    # still equal to the auto-generated default as "not set".
    from hyperdjango.conf import DEFAULTS, get_setting

    csrf = str(get_setting("CSRF_SECRET", "") or "")
    if not csrf or csrf == DEFAULTS.get("CSRF_SECRET"):
        return [
            CheckResult(
                name="csrf_secret",
                category="security",
                status=CheckStatus.WARN,
                message="CSRF_SECRET not set (using per-process random default)",
                hint="Set HYPER_CSRF_SECRET env var (or HYPERDJANGO_CSRF_SECRET) for production",
            )
        ]
    return [
        CheckResult(
            name="csrf_secret",
            category="security",
            status=CheckStatus.PASS,
            message="CSRF_SECRET configured",
        )
    ]


@doctor_check("security", "session_cookies", order=40)
def check_session_cookies(ctx: DoctorContext) -> list[CheckResult]:
    from hyperdjango.conf import get_setting

    # Resolve effective settings the way the app does (Django override >
    # HYPER_* env > default) rather than reading bare env names the framework
    # never consults. Defaults: SESSION_COOKIE_SECURE=False, HTTPONLY=True.
    secure = bool(get_setting("SESSION_COOKIE_SECURE", False))
    httponly = bool(get_setting("SESSION_COOKIE_HTTPONLY", True))

    issues: list[str] = []
    if not secure:
        issues.append("SESSION_COOKIE_SECURE not set")
    if not httponly:
        issues.append("SESSION_COOKIE_HTTPONLY disabled")

    if not issues:
        return [
            CheckResult(
                name="session_cookies",
                category="security",
                status=CheckStatus.PASS,
                message="Session cookies: Secure + HttpOnly",
            )
        ]
    return [
        CheckResult(
            name="session_cookies",
            category="security",
            status=CheckStatus.WARN,
            message="; ".join(issues),
            hint="Set for production: HYPER_SESSION_COOKIE_SECURE=1",
        )
    ]


@doctor_check("security", "rbac_group_coverage", order=50)
def check_rbac_group_coverage(ctx: DoctorContext) -> list[CheckResult]:
    """Warn if users have is_staff/is_superuser=True without RBAC group membership."""
    if ctx.db_handle < 0:
        return [
            CheckResult(
                name="rbac_group_coverage",
                category="security",
                status=CheckStatus.SKIP,
                message="No database connection",
            )
        ]

    from hyperdjango._hyperdjango_native import _db_query

    # Check if hyper_users table exists
    try:
        rows = _db_query(
            ctx.db_handle,
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'hyper_users')",
            [],
        )
        # _db_query returns positional tuples; the EXISTS boolean is column 0.
        # The old ``rows[0].get("exists")`` raised AttributeError on a tuple,
        # which the except below swallowed → this check always SKIPped.
        if not rows or not rows[0][0]:
            return [
                CheckResult(
                    name="rbac_group_coverage",
                    category="security",
                    status=CheckStatus.SKIP,
                    message="hyper_users table not found",
                )
            ]
    # blind-except: the optional hyper_users table probe degrades to a SKIP result when the query can't run (table absent, DB unreachable).
    except Exception:
        return [
            CheckResult(
                name="rbac_group_coverage",
                category="security",
                status=CheckStatus.SKIP,
                message="Could not check hyper_users table",
            )
        ]

    # Find users with boolean flags but no matching RBAC group
    results: list[CheckResult] = []
    with contextlib.suppress(Exception):
        orphaned = _db_query(
            ctx.db_handle,
            """
            SELECT u.id, u.username,
                   u.is_staff, u.is_superuser
            FROM hyper_users u
            WHERE (u.is_staff = TRUE OR u.is_superuser = TRUE)
              AND NOT EXISTS (
                SELECT 1 FROM hyper_user_groups ug
                JOIN hyper_groups g ON g.id = ug.group_id
                WHERE ug.user_id = u.id
                  AND g.name IN ('staff', 'superuser')
              )
            """,
            [],
        )
        if orphaned:
            # Row columns are positional: (id, username, is_staff, is_superuser)
            # — username is index 1. The old ``r["username"]`` raised on the
            # tuple and was swallowed by contextlib.suppress above.
            usernames = ", ".join(str(r[1]) for r in orphaned)
            results.append(
                CheckResult(
                    name="rbac_group_coverage",
                    category="security",
                    status=CheckStatus.WARN,
                    message=f"{len(orphaned)} user(s) have is_staff/is_superuser without RBAC groups: {usernames}",
                    hint="Run 'hyper createsuperuser' to auto-assign RBAC groups, or use PermissionChecker.add_user_to_group()",
                )
            )
        else:
            results.append(
                CheckResult(
                    name="rbac_group_coverage",
                    category="security",
                    status=CheckStatus.PASS,
                    message="All staff/superuser flags have matching RBAC groups",
                )
            )
    if not results:
        results.append(
            CheckResult(
                name="rbac_group_coverage",
                category="security",
                status=CheckStatus.SKIP,
                message="Could not query RBAC group coverage",
            )
        )
    return results


@doctor_check("security", "debug_mode", order=15)
def check_debug_mode(ctx: DoctorContext) -> list[CheckResult]:
    """DEBUG must be OFF in production — the debug error page renders exception
    tracebacks and request details (information disclosure)."""
    from hyperdjango.conf import get_setting

    if bool(get_setting("DEBUG", False)):
        return [
            CheckResult(
                name="debug_mode",
                category="security",
                status=CheckStatus.WARN,
                message="DEBUG is ON — error pages leak tracebacks + request data",
                hint="Set DEBUG off in production (HYPER_DEBUG=0 / HYPERDJANGO_DEBUG=False)",
            )
        ]
    return [
        CheckResult(
            name="debug_mode",
            category="security",
            status=CheckStatus.PASS,
            message="DEBUG is off",
        )
    ]


def _check_random_secret(setting: str, env: str) -> CheckResult:
    """Flag a security secret still equal to its per-process random default."""
    from hyperdjango.conf import DEFAULTS, get_setting

    value = str(get_setting(setting, "") or "")
    name = setting.lower()
    if not value or value == DEFAULTS.get(setting):
        return CheckResult(
            name=name,
            category="security",
            status=CheckStatus.WARN,
            message=f"{setting} not set (using per-process random default)",
            hint=f"Set {env} for production (sessions/tokens survive restarts + scale out)",
        )
    return CheckResult(
        name=name,
        category="security",
        status=CheckStatus.PASS,
        message=f"{setting} configured",
    )


@doctor_check("security", "session_secret", order=35)
def check_session_secret(ctx: DoctorContext) -> list[CheckResult]:
    return [_check_random_secret("SESSION_SECRET", "HYPER_SESSION_SECRET")]


@doctor_check("security", "admin_secret", order=36)
def check_admin_secret(ctx: DoctorContext) -> list[CheckResult]:
    return [_check_random_secret("ADMIN_SECRET", "HYPER_ADMIN_SECRET")]


@doctor_check("security", "allowed_hosts", order=45)
def check_allowed_hosts(ctx: DoctorContext) -> list[CheckResult]:
    """An empty or wildcard ALLOWED_HOSTS lets a forged Host header drive
    host-derived redirects/absolute URLs (Host-header attacks)."""
    from hyperdjango.conf import get_setting

    hosts = get_setting("ALLOWED_HOSTS", []) or []
    if not hosts:
        return [
            CheckResult(
                name="allowed_hosts",
                category="security",
                status=CheckStatus.WARN,
                message="ALLOWED_HOSTS is empty (any Host header accepted)",
                hint="Set ALLOWED_HOSTS to your domain(s) for production",
            )
        ]
    if "*" in hosts:
        return [
            CheckResult(
                name="allowed_hosts",
                category="security",
                status=CheckStatus.WARN,
                message="ALLOWED_HOSTS contains '*' (any Host header accepted)",
                hint="List explicit domain(s) instead of '*' for production",
            )
        ]
    return [
        CheckResult(
            name="allowed_hosts",
            category="security",
            status=CheckStatus.PASS,
            message=f"ALLOWED_HOSTS set ({len(hosts)} host(s))",
        )
    ]
