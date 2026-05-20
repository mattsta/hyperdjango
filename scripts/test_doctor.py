"""
Tests for `hyper doctor` diagnostic tool.

Run: uv run hyper-test doctor
"""

# hyper-test: unit

import json
import os

passed = 0
failed = 0
errors: list[str] = []


def check(name, condition, msg=""):
    global passed, failed
    if condition:
        passed += 1
    else:
        failed += 1
        err = f"FAIL: {name}"
        if msg:
            err += f" — {msg}"
        errors.append(err)
        print(f"  ✗ {name} {msg}")


def main():
    from hyperdjango.doctor import (
        CheckResult,
        CheckStatus,
        DoctorReport,
        doctor_check,
        run_doctor,
    )
    from hyperdjango.doctor._registry import get_all_categories, get_checks

    print("=" * 60)
    print("Hyper Doctor Tests")
    print("=" * 60)

    # ── Registry tests ──
    print("\n── Registry ──")

    categories = get_all_categories()
    check("categories registered", len(categories) >= 7, f"got {len(categories)}")
    check("build category exists", "build" in categories)
    check("python category exists", "python" in categories)
    check("database category exists", "database" in categories)
    check("perf category exists", "perf" in categories)
    check("config category exists", "config" in categories)
    check("filesystem category exists", "filesystem" in categories)
    check("security category exists", "security" in categories)

    build_checks = get_checks("build")
    check("build has checks", len(build_checks) >= 3)

    python_checks = get_checks("python")
    check("python has checks", len(python_checks) >= 3)

    # ── Run without database ──
    print("\n── Run (no database) ──")

    report = run_doctor(skip_db=True, output_format="ci")
    check("report is DoctorReport", isinstance(report, DoctorReport))
    check("report has categories", len(report.categories) >= 6)
    check("total_passed > 0", report.total_passed > 0)
    check("total_failed == 0", report.total_failed == 0, f"got {report.total_failed}")
    check("total_duration > 0", report.total_duration_ns > 0)
    check("version set", report.hyperdjango_version != "")
    check("python version set", report.python_version != "")
    check("timestamp set", report.timestamp != "")

    # ── Build checks ──
    print("\n── Build Checks ──")

    build_cat = next((c for c in report.categories if c.name == "build"), None)
    check("build category in report", build_cat is not None)
    if build_cat:
        check(
            "native extension passed",
            any(
                c.name == "native_extension" and c.status == CheckStatus.PASS
                for c in build_cat.checks
            ),
        )
        check(
            "abi match passed",
            any(
                c.name == "abi_match" and c.status == CheckStatus.PASS
                for c in build_cat.checks
            ),
        )

    # ── Python checks ──
    print("\n── Python Checks ──")

    python_cat = next((c for c in report.categories if c.name == "python"), None)
    check("python category in report", python_cat is not None)
    if python_cat:
        check(
            "python version passed",
            any(
                c.name == "python_version" and c.status == CheckStatus.PASS
                for c in python_cat.checks
            ),
        )
        check(
            "free threaded passed",
            any(
                c.name == "free_threaded" and c.status == CheckStatus.PASS
                for c in python_cat.checks
            ),
        )

    # ── Database checks skipped ──
    print("\n── Database Skip ──")

    db_cat = next((c for c in report.categories if c.name == "database"), None)
    check("database category in report", db_cat is not None)
    if db_cat:
        check(
            "all db checks skipped",
            all(c.status == CheckStatus.SKIP for c in db_cat.checks),
        )

    # ── Performance checks ──
    print("\n── Performance Checks ──")

    perf_cat = next((c for c in report.categories if c.name == "perf"), None)
    check("perf category in report", perf_cat is not None)
    if perf_cat:
        check(
            "json speed passed",
            any(
                c.name == "json_speed" and c.status == CheckStatus.PASS
                for c in perf_cat.checks
            ),
        )
        check(
            "simd validation passed",
            any(
                c.name == "simd_validation" and c.status == CheckStatus.PASS
                for c in perf_cat.checks
            ),
        )

    # ── Security checks ──
    print("\n── Security Checks ──")

    sec_cat = next((c for c in report.categories if c.name == "security"), None)
    check("security category in report", sec_cat is not None)
    if sec_cat:
        check(
            "argon2 passed",
            any(
                c.name == "argon2_available" and c.status == CheckStatus.PASS
                for c in sec_cat.checks
            ),
        )

    # ── JSON output ──
    print("\n── JSON Output ──")

    import io
    import sys

    old_stdout = sys.stdout
    sys.stdout = captured = io.StringIO()
    run_doctor(skip_db=True, output_format="json")
    sys.stdout = old_stdout
    json_output = captured.getvalue()

    parsed = json.loads(json_output)
    check("JSON is valid", isinstance(parsed, dict))
    check("JSON has version", "version" in parsed)
    check("JSON has summary", "summary" in parsed)
    check("JSON has categories", "categories" in parsed)
    check("JSON categories count", len(parsed["categories"]) >= 6)

    # ── Category filter ──
    print("\n── Category Filter ──")

    filtered = run_doctor(skip_db=True, output_format="ci", category_filter="build")
    check("filtered has 1 category", len(filtered.categories) == 1)
    check("filtered is build", filtered.categories[0].name == "build")

    # ── Custom check registration ──
    print("\n── Custom Check Registration ──")

    @doctor_check("custom", "test_custom_check")
    def custom_check(ctx):
        return [
            CheckResult(
                name="test_custom_check",
                category="custom",
                status=CheckStatus.PASS,
                message="Custom check works",
            )
        ]

    custom_checks = get_checks("custom")
    check("custom check registered", len(custom_checks) == 1)
    check("custom check name", custom_checks[0].name == "test_custom_check")

    # ── Live database tests (if DATABASE_URL set) ──
    db_url = os.environ.get("DATABASE_URL", "")
    if db_url:
        print("\n── Live Database ──")
        db_report = run_doctor(database_url=db_url, output_format="ci")
        db_cat = next((c for c in db_report.categories if c.name == "database"), None)
        if db_cat:
            check(
                "db connect passed",
                any(
                    c.name == "db_connect" and c.status == CheckStatus.PASS
                    for c in db_cat.checks
                ),
            )
            check(
                "pg version passed",
                any(
                    c.name == "pg_version" and c.status == CheckStatus.PASS
                    for c in db_cat.checks
                ),
            )
            check(
                "query latency passed",
                any(
                    c.name == "query_latency" and c.status == CheckStatus.PASS
                    for c in db_cat.checks
                ),
            )
            # REGRESSION (finding #3a): with extensions ENABLED, the extensions
            # check must not crash indexing a tuple as a dict.
            check(
                "extensions check does not crash on enabled extensions",
                not any(
                    c.name == "extensions" and c.status == CheckStatus.FAIL
                    for c in db_cat.checks
                )
                and not any("crashed" in c.message.lower() for c in db_cat.checks),
            )
        # REGRESSION (finding #3b): rbac_group_coverage must actually RUN
        # (tuple indexing), i.e. never crash — SKIP is fine when tables absent.
        sec_cat = next((c for c in db_report.categories if c.name == "security"), None)
        if sec_cat:
            rbac = next(
                (c for c in sec_cat.checks if c.name == "rbac_group_coverage"), None
            )
            check(
                "rbac_group_coverage produced a result (ran, no crash)",
                rbac is not None and rbac.status != CheckStatus.FAIL,
            )
    else:
        print("\n  ⚠ Skipping live DB tests (no DATABASE_URL)")

    # ── Config checks read EFFECTIVE settings, not DEFAULTS (finding #3d) ──
    print("\n── Config effective-settings ──")
    import unittest.mock as _mock

    with _mock.patch.dict(os.environ, {"HYPER_POOL_SIZE": "4"}):
        # Clear the env-override cache so get_setting re-reads HYPER_POOL_SIZE.
        import hyperdjango.conf as _conf

        _conf._ENV_OVERRIDES_POPULATED = False
        _conf._ENV_OVERRIDES.clear()
        cfg_report = run_doctor(output_format="ci", category_filter="config")
        _conf._ENV_OVERRIDES_POPULATED = False
        _conf._ENV_OVERRIDES.clear()

    cfg = next((c for c in cfg_report.categories if c.name == "config"), None)
    check("config category ran", cfg is not None)
    if cfg:
        ptc = next(
            (c for c in cfg.checks if c.name == "pool_size_vs_thread_count"), None
        )
        check(
            "HYPER_POOL_SIZE=4 flagged as pathological (reads get_setting, not DEFAULTS)",
            ptc is not None and ptc.status == CheckStatus.FAIL,
        )
        # finding #3f: both pool checks agree on the effective size.
        pvc = next((c for c in cfg.checks if c.name == "pool_size_vs_cpu"), None)
        check(
            "pool_size_vs_cpu also reports the manual size 4 (reconciled formula)",
            pvc is not None and "4" in pvc.message,
        )
        # finding #3e: new HTTP-tuning checks are registered.
        names = {c.name for c in cfg.checks}
        check(
            "new HTTP tuning checks registered",
            {"http_server_model", "listen_backlog", "fd_limit", "send_timeout"}
            <= names,
        )

    # New HTTP tuning checks flag misconfigs (finding #3e).
    print("\n── HTTP tuning misconfig detection ──")
    with _mock.patch.dict(
        os.environ,
        {"HYPER_LISTEN_BACKLOG": "999999", "HYPER_SEND_TIMEOUT_MS": "0"},
    ):
        http_report = run_doctor(output_format="ci", category_filter="config")
    hcfg = next((c for c in http_report.categories if c.name == "config"), None)
    if hcfg:
        lb = next((c for c in hcfg.checks if c.name == "listen_backlog"), None)
        st = next((c for c in hcfg.checks if c.name == "send_timeout"), None)
        # backlog vs somaxconn only warns when somaxconn is readable
        check(
            "huge backlog flagged (or somaxconn unreadable)",
            lb is not None and lb.status in (CheckStatus.WARN, CheckStatus.PASS),
        )
        check(
            "send_timeout=0 flagged as unbounded",
            st is not None and st.status == CheckStatus.WARN,
        )

    # ── --ci exit-code gate (end-to-end via subprocess) ──
    # Any non-PASS security check must fail the pipeline (exit 1) AND say so
    # explicitly — a "0 failed" summary followed by a silent exit 1 reads as
    # a crash. With a production-shaped config every security check passes
    # and the gate exits 0.
    print("\n── CI gate (subprocess) ──")
    import subprocess
    import sys as _sys

    bare_env = {
        k: v
        for k, v in os.environ.items()
        # Strip any secrets from the caller's env so the WARN path is
        # deterministic regardless of the developer's shell config.
        if not k.startswith(("HYPER_", "HYPERDJANGO_"))
    }
    gate_cmd = [_sys.executable, "-m", "hyperdjango.cli", "doctor", "--ci", "--no-db"]

    ungated = subprocess.run(
        gate_cmd, env=bare_env, capture_output=True, text=True, timeout=120
    )
    combined = ungated.stdout + ungated.stderr
    check(
        "--ci exits 1 when security checks warn",
        ungated.returncode == 1,
        f"got {ungated.returncode}",
    )
    check(
        "--ci names the gating security checks",
        "CI gate:" in combined and "security check(s)" in combined,
        f"output tail: {combined[-200:]!r}",
    )

    prod_env = bare_env | {
        "HYPER_SECRET_KEY": "test-only-secret-key-0123456789abcdef012345",
        "HYPER_CSRF_SECRET": "test-only-csrf-secret-0123456789abcdef0123",
        "HYPER_SESSION_SECRET": "test-only-session-secret-0123456789abcdef",
        "HYPER_ADMIN_SECRET": "test-only-admin-secret-0123456789abcdef01",
        "HYPER_SESSION_COOKIE_SECURE": "1",
        "HYPER_ALLOWED_HOSTS": "localhost,127.0.0.1",
    }
    gated = subprocess.run(
        gate_cmd, env=prod_env, capture_output=True, text=True, timeout=120
    )
    check(
        "--ci exits 0 with production-shaped security config",
        gated.returncode == 0,
        f"got {gated.returncode}: {(gated.stdout + gated.stderr)[-300:]!r}",
    )

    # ── Summary ──
    print("\n" + "=" * 60)
    total = passed + failed
    print(f"Results: {passed}/{total} passed, {failed} failed")
    if errors:
        print("\nFailures:")
        for e in errors:
            print(f"  {e}")
    print("=" * 60)

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    import sys

    sys.exit(main())
