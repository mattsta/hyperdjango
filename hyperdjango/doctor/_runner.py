"""Doctor runner — executes all checks and builds the report."""

import sys
import time
from datetime import UTC, datetime

from hyperdjango.doctor._output import render_ci, render_json, render_terminal
from hyperdjango.doctor._registry import (
    CATEGORY_NAMES,
    CATEGORY_ORDER,
    CategorySummary,
    CheckResult,
    CheckStatus,
    DoctorContext,
    DoctorReport,
    get_checks,
)


def run_doctor(
    database_url: str = "",
    verbose: bool = False,
    skip_db: bool = False,
    output_format: str = "terminal",
    category_filter: str = "",
) -> DoctorReport:
    """Execute all registered doctor checks and return a report."""
    import hyperdjango

    ctx = DoctorContext(
        database_url=database_url,
        verbose=verbose,
        skip_db=skip_db,
        category_filter=category_filter,
    )

    categories: list[CategorySummary] = []
    total_start = time.perf_counter_ns()

    for cat_name in CATEGORY_ORDER:
        if category_filter and cat_name != category_filter:
            continue

        checks = get_checks(cat_name)
        if not checks:
            continue

        cat_results: list[CheckResult] = []
        cat_start = time.perf_counter_ns()

        for registered in checks:
            check_start = time.perf_counter_ns()
            try:
                results = registered.func(ctx)
                for r in results:
                    r.duration_ns = time.perf_counter_ns() - check_start
                cat_results.extend(results)
            # blind-except: a diagnostic check that crashes is reported as a FAIL result so the doctor keeps running the remaining checks.
            except Exception as e:
                cat_results.append(
                    CheckResult(
                        name=registered.name,
                        category=cat_name,
                        status=CheckStatus.FAIL,
                        message=f"Check crashed: {type(e).__name__}: {e}",
                        duration_ns=time.perf_counter_ns() - check_start,
                    )
                )

        cat_duration = time.perf_counter_ns() - cat_start

        summary = CategorySummary(
            name=cat_name,
            display_name=CATEGORY_NAMES.get(cat_name, cat_name),
            checks=cat_results,
            passed=sum(1 for r in cat_results if r.status == CheckStatus.PASS),
            warned=sum(1 for r in cat_results if r.status == CheckStatus.WARN),
            failed=sum(1 for r in cat_results if r.status == CheckStatus.FAIL),
            skipped=sum(1 for r in cat_results if r.status == CheckStatus.SKIP),
            duration_ns=cat_duration,
        )
        categories.append(summary)

    # Cleanup DB connection if we opened one
    if ctx.db_handle >= 0:
        try:
            from hyperdjango._hyperdjango_native import _db_close_pool

            _db_close_pool(ctx.db_handle)
        # blind-except: best-effort cleanup of the diagnostic DB pool; a close failure must not abort generation of the doctor report.
        except Exception:
            pass

    total_duration = time.perf_counter_ns() - total_start
    v = sys.version_info

    report = DoctorReport(
        categories=categories,
        total_passed=sum(c.passed for c in categories),
        total_warned=sum(c.warned for c in categories),
        total_failed=sum(c.failed for c in categories),
        total_skipped=sum(c.skipped for c in categories),
        total_duration_ns=total_duration,
        hyperdjango_version=hyperdjango.__version__,
        python_version=f"{v.major}.{v.minor}.{v.micro}",
        timestamp=datetime.now(UTC).isoformat(),
    )

    # Render output
    if output_format == "json":
        render_json(report)
    elif output_format == "ci":
        render_ci(report)
    else:
        render_terminal(report)

    return report
