"""Doctor checks: Performance Readiness."""

import json as _stdlib_json
import os
import time

from hyperdjango.doctor._registry import (
    CheckResult,
    CheckStatus,
    DoctorContext,
    doctor_check,
)


@doctor_check("perf", "json_speed", order=10)
def check_json_speed(ctx: DoctorContext) -> list[CheckResult]:
    from hyperdjango.native import fast_json_dumps

    test_data = {
        "id": 42,
        "name": "Alice",
        "email": "alice@example.com",
        "active": True,
    }
    n = 1000

    start = time.perf_counter_ns()
    for _ in range(n):
        fast_json_dumps(test_data)
    native_ns = (time.perf_counter_ns() - start) / n

    start = time.perf_counter_ns()
    for _ in range(n):
        _stdlib_json.dumps(test_data)
    stdlib_ns = (time.perf_counter_ns() - start) / n

    speedup = stdlib_ns / native_ns if native_ns > 0 else 0
    return [
        CheckResult(
            name="json_speed",
            category="perf",
            status=CheckStatus.PASS,
            message=f"JSON native {speedup:.1f}x faster",
            detail=f"{native_ns:.0f}ns vs {stdlib_ns:.0f}ns (stdlib)",
        )
    ]


@doctor_check("perf", "simd_validation", order=20)
def check_simd_validation(ctx: DoctorContext) -> list[CheckResult]:
    from hyperdjango._hyperdjango_native import validate_email

    n = 1000
    start = time.perf_counter_ns()
    for _ in range(n):
        validate_email("user@example.com")
    ns_per = (time.perf_counter_ns() - start) / n

    return [
        CheckResult(
            name="simd_validation",
            category="perf",
            status=CheckStatus.PASS,
            message=f"SIMD email validation: {ns_per:.0f}ns/call",
        )
    ]


@doctor_check("perf", "template_speed", order=30)
def check_template_speed(ctx: DoctorContext) -> list[CheckResult]:
    from hyperdjango._hyperdjango_native import (
        _template_compile,
        _template_render,
    )

    source = "Hello {{ name }}! You have {{ count }} messages."
    n = 1000

    capsule = _template_compile(source, "<bench>")

    start = time.perf_counter_ns()
    for _ in range(n):
        _template_render(capsule, {"name": "Alice", "count": 42})
    render_ns = (time.perf_counter_ns() - start) / n

    return [
        CheckResult(
            name="template_speed",
            category="perf",
            status=CheckStatus.PASS,
            message=f"Template render: {render_ns / 1000:.1f}us/render",
        )
    ]


@doctor_check("perf", "thread_pool_config", order=40)
def check_thread_pool_config(ctx: DoctorContext) -> list[CheckResult]:
    from hyperdjango.conf import DEFAULT_THREAD_POOL_SIZE, get_setting

    actual = int(
        get_setting("THREAD_POOL_SIZE", DEFAULT_THREAD_POOL_SIZE)
        or DEFAULT_THREAD_POOL_SIZE
    )
    cpu_count = os.cpu_count() or 4

    status = CheckStatus.PASS
    if actual < cpu_count:
        status = CheckStatus.WARN

    return [
        CheckResult(
            name="thread_pool_config",
            category="perf",
            status=status,
            message=f"Thread pool: {actual} threads ({cpu_count} CPU cores)",
            hint=""
            if status == CheckStatus.PASS
            else f"Set HYPER_THREAD_POOL_SIZE={cpu_count * 2}",
        )
    ]


@doctor_check("perf", "cpufreq_governor", order=120)
def check_cpufreq_governor(ctx: DoctorContext) -> list[CheckResult]:
    """Non-`performance` cpufreq governors (schedutil/powersave) let
    partially-loaded cores idle near minimum frequency, which shows up as
    inconsistent latency under bursty load and non-monotonic scaling in
    benchmarks (observed: a 6-10x throughput swing at fixed configuration).
    Deliberate for power-managed hosts — hence WARN with context, not FAIL."""
    from pathlib import Path

    gov_path = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
    try:
        gov = gov_path.read_text(encoding="ascii").strip()
    except OSError:
        return [
            CheckResult(
                name="cpufreq_governor",
                category="perf",
                status=CheckStatus.SKIP,
                message="cpufreq governor not inspectable (non-Linux / no cpufreq)",
            )
        ]
    ok = gov == "performance"
    return [
        CheckResult(
            name="cpufreq_governor",
            category="perf",
            status=CheckStatus.PASS if ok else CheckStatus.WARN,
            message=f"cpufreq governor: {gov}",
            hint=(
                ""
                if ok
                else "for consistent latency / benchmarking: echo performance | "
                "sudo tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor"
            ),
        )
    ]
