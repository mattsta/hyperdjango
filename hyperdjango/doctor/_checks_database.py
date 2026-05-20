"""Doctor checks: Database Connectivity."""

import time

from hyperdjango.doctor._registry import (
    CheckResult,
    CheckStatus,
    DoctorContext,
    doctor_check,
)


@doctor_check("database", "db_connect", order=10)
def check_db_connect(ctx: DoctorContext) -> list[CheckResult]:
    if ctx.skip_db or not ctx.database_url:
        return [
            CheckResult(
                name="db_connect",
                category="database",
                status=CheckStatus.SKIP,
                message="Database checks skipped"
                if ctx.skip_db
                else "No DATABASE_URL set",
            )
        ]

    from hyperdjango._hyperdjango_native import _db_configure, _db_query

    try:
        start = time.perf_counter()
        handle = _db_configure(ctx.database_url, 2, 5000, 0)
        _db_query(handle, "SELECT 1", [])
        latency_ms = (time.perf_counter() - start) * 1000
        ctx.db_handle = handle
        return [
            CheckResult(
                name="db_connect",
                category="database",
                status=CheckStatus.PASS,
                message="PostgreSQL connected",
                metric_value=f"{latency_ms:.1f}ms",
            )
        ]
    # blind-except: the connectivity check reports any connection failure as a FAIL result with a hint, not raising and aborting the doctor run.
    except Exception as e:
        return [
            CheckResult(
                name="db_connect",
                category="database",
                status=CheckStatus.FAIL,
                message=f"Connection failed: {e}",
                hint=f"Check DATABASE_URL: {ctx.database_url[:40]}...",
            )
        ]


@doctor_check("database", "pg_version", order=20)
def check_pg_version(ctx: DoctorContext) -> list[CheckResult]:
    if ctx.db_handle < 0:
        return [
            CheckResult(
                name="pg_version",
                category="database",
                status=CheckStatus.SKIP,
                message="No DB connection",
            )
        ]

    from hyperdjango._hyperdjango_native import _db_query

    rows = _db_query(ctx.db_handle, "SELECT version()", [])
    version_str = rows[0][0] if rows else "unknown"
    # Extract major version number
    parts = version_str.split()
    pg_version = parts[1] if len(parts) > 1 else version_str
    major = int(pg_version.split(".")[0]) if pg_version[0].isdigit() else 0

    status = CheckStatus.PASS if major >= 16 else CheckStatus.WARN
    return [
        CheckResult(
            name="pg_version",
            category="database",
            status=status,
            message=f"PostgreSQL {pg_version}",
            hint="" if major >= 16 else "PostgreSQL 16+ recommended",
        )
    ]


@doctor_check("database", "pool_stats", order=30)
def check_pool_stats(ctx: DoctorContext) -> list[CheckResult]:
    if ctx.db_handle < 0:
        return [
            CheckResult(
                name="pool_stats",
                category="database",
                status=CheckStatus.SKIP,
                message="No DB connection",
            )
        ]

    from hyperdjango._hyperdjango_native import _db_pool_stats

    stats = _db_pool_stats(ctx.db_handle)
    total = stats.get("total", 0)
    available = stats.get("available", 0)
    in_use = stats.get("in_use", 0)

    status = CheckStatus.PASS if available > 0 else CheckStatus.WARN
    return [
        CheckResult(
            name="pool_stats",
            category="database",
            status=status,
            message=f"Pool: {total} total, {available} available, {in_use} in-use",
        )
    ]


@doctor_check("database", "stmt_cache", order=40)
def check_stmt_cache(ctx: DoctorContext) -> list[CheckResult]:
    if ctx.db_handle < 0:
        return [
            CheckResult(
                name="stmt_cache",
                category="database",
                status=CheckStatus.SKIP,
                message="No DB connection",
            )
        ]

    from hyperdjango._hyperdjango_native import _db_stmt_cache_stats

    stats = _db_stmt_cache_stats()
    hits = stats.get("hits", 0)
    misses = stats.get("misses", 0)
    entries = stats.get("entries", 0)
    total = hits + misses
    hit_rate = (hits / total * 100) if total > 0 else 0.0

    status = CheckStatus.PASS
    if total > 100 and hit_rate < 50:
        status = CheckStatus.WARN

    return [
        CheckResult(
            name="stmt_cache",
            category="database",
            status=status,
            message=f"Stmt cache: {hit_rate:.1f}% hit rate",
            detail=f"{hits} hits / {total} lookups, {entries} entries",
        )
    ]


@doctor_check("database", "extensions", order=50)
def check_extensions(ctx: DoctorContext) -> list[CheckResult]:
    """Check every extension declared in db_extensions.REGISTRY.

    Replaces the old pgvector-only check — driven by the same registry
    that `hyper db extensions ensure` and CI use, so adding a new
    feature-required extension automatically gets a doctor check.
    """
    if ctx.db_handle < 0:
        return [
            CheckResult(
                name="extensions",
                category="database",
                status=CheckStatus.SKIP,
                message="No DB connection",
            )
        ]

    from hyperdjango._hyperdjango_native import _db_query
    from hyperdjango.db_extensions import REGISTRY

    results: list[CheckResult] = []
    for ext in REGISTRY:
        check_name = f"ext_{ext.name}"
        try:
            avail = _db_query(
                ctx.db_handle,
                "SELECT 1 FROM pg_available_extensions WHERE name = $1",
                [ext.name],
            )
            enabled = _db_query(
                ctx.db_handle,
                "SELECT extversion FROM pg_extension WHERE extname = $1",
                [ext.name],
            )
        # blind-except: a per-extension probe query that fails is reported as a SKIP result and the loop continues with the remaining extensions.
        except Exception as e:
            results.append(
                CheckResult(
                    name=check_name,
                    category="database",
                    status=CheckStatus.SKIP,
                    message=f"{ext.name}: query failed ({e})",
                )
            )
            continue

        if not avail:
            hint_lines = [f"Required by: {', '.join(ext.required_by) or 'optional'}"]
            if ext.docker_image:
                hint_lines.append(f"Docker: {ext.docker_image}")
            if ext.apt_package:
                hint_lines.append(f"apt: install {ext.apt_package}")
            results.append(
                CheckResult(
                    name=check_name,
                    category="database",
                    status=CheckStatus.WARN,
                    message=f"{ext.name}: binary not installed in postgres server",
                    hint=" | ".join(hint_lines),
                )
            )
        elif not enabled:
            results.append(
                CheckResult(
                    name=check_name,
                    category="database",
                    status=CheckStatus.WARN,
                    message=f"{ext.name}: available but not enabled",
                    hint="Run: hyper db extensions ensure",
                )
            )
        else:
            # _db_query returns rows as positional tuples, not dicts — index
            # by position (SELECT extversion → column 0), never by key.
            version = enabled[0][0] if enabled and enabled[0] else "?"
            results.append(
                CheckResult(
                    name=check_name,
                    category="database",
                    status=CheckStatus.PASS,
                    message=f"{ext.name} v{version} enabled",
                )
            )
    return results


@doctor_check("database", "query_latency", order=60)
def check_query_latency(ctx: DoctorContext) -> list[CheckResult]:
    if ctx.db_handle < 0:
        return [
            CheckResult(
                name="query_latency",
                category="database",
                status=CheckStatus.SKIP,
                message="No DB connection",
            )
        ]

    from hyperdjango._hyperdjango_native import _db_query

    times: list[float] = []
    for _ in range(5):
        start = time.perf_counter()
        _db_query(ctx.db_handle, "SELECT 1", [])
        times.append((time.perf_counter() - start) * 1000)

    times.sort()
    p50 = times[len(times) // 2]
    status = CheckStatus.PASS if p50 < 10 else CheckStatus.WARN

    return [
        CheckResult(
            name="query_latency",
            category="database",
            status=status,
            message=f"Query latency p50: {p50:.2f}ms",
            hint="" if p50 < 10 else "High latency — check network/connection",
        )
    ]


@doctor_check("database", "pgvector_trusted", order=200)
def check_pgvector_trusted(ctx: DoctorContext) -> list[CheckResult]:
    """pgvector installations that omit upstream's `trusted = true` control
    flag require SUPERUSER for CREATE EXTENSION — every VectorField setup and
    per-database extension bootstrap then fails with "permission denied to
    create extension" for normal roles. `scripts/dev_bootstrap.sh` marks the
    control file trusted when it can."""
    import subprocess as _sp
    from pathlib import Path

    from hyperdjango.conf import resolve_database_url

    # Same gate as every database-category check: no configured database →
    # SKIP (the category's invariant is "all db checks skip without a DB").
    if not (ctx.database_url or resolve_database_url()):
        return [
            CheckResult(
                name="pgvector_trusted",
                category="database",
                status=CheckStatus.SKIP,
                message="no database configured",
            )
        ]

    candidates: list[Path] = []
    try:
        sharedir = _sp.run(
            ["pg_config", "--sharedir"], capture_output=True, text=True, timeout=5
        ).stdout.strip()
        if sharedir:
            candidates.append(Path(sharedir) / "extension" / "vector.control")
    except OSError, _sp.SubprocessError:
        pass
    candidates.extend(Path("/usr/share/postgresql").glob("*/extension/vector.control"))

    for p in candidates:
        cand = str(p)
        if not p.is_file():
            continue
        try:
            trusted = any(
                line.strip().startswith("trusted")
                for line in p.read_text().splitlines()
            )
        except OSError:
            continue
        return [
            CheckResult(
                name="pgvector_trusted",
                category="database",
                status=CheckStatus.PASS if trusted else CheckStatus.WARN,
                message=(
                    f"pgvector control trusted ({cand})"
                    if trusted
                    else f"pgvector NOT marked trusted ({cand}) — CREATE "
                    "EXTENSION needs superuser; VectorField setup fails for "
                    "normal roles"
                ),
                hint=(
                    ""
                    if trusted
                    else "bash scripts/dev_bootstrap.sh  (marks it trusted), or: "
                    f"echo 'trusted = true' | sudo tee -a {cand}"
                ),
            )
        ]
    return [
        CheckResult(
            name="pgvector_trusted",
            category="database",
            status=CheckStatus.SKIP,
            message="pgvector control file not found (extension not installed)",
        )
    ]
