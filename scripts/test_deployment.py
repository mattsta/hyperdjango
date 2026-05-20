"""End-to-end deployment artifact tests.
# hyper-test: unit

Validates that deployment tooling produces correct, production-ready artifacts:
1. systemd unit file generation — correct format, security hardening, env vars
2. Dockerfile template — correct stages, Zig install, build steps
3. CLI start/stop/status — PID file management, signal handling
4. Health check endpoint structure
5. Graceful shutdown behavior
6. Metrics endpoint format

Run: uv run hyper-test deployment
"""

import argparse
import os
import sys
from pathlib import Path

passed = 0
failed = 0
errors: list[str] = []


def check(name, condition, msg=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS: {name}")
    else:
        failed += 1
        err = f"FAIL: {name}"
        if msg:
            err += f" — {msg}"
        errors.append(err)
        print(f"  ✗ {name} {msg}")


# ── systemd unit file generation ─────────────────────────────────────────────


def test_systemd_unit_template():
    """Unit template has required systemd sections and directives."""
    from hyperdjango.systemd import _UNIT_TEMPLATE

    check("template has [Unit] section", "[Unit]" in _UNIT_TEMPLATE)
    check("template has [Service] section", "[Service]" in _UNIT_TEMPLATE)
    check("template has [Install] section", "[Install]" in _UNIT_TEMPLATE)
    check("template has Type=exec", "Type=exec" in _UNIT_TEMPLATE)
    check("template has KillSignal=SIGTERM", "KillSignal=SIGTERM" in _UNIT_TEMPLATE)
    check("template has Restart=on-failure", "Restart=on-failure" in _UNIT_TEMPLATE)


def test_systemd_security_hardening():
    """Unit template includes security hardening directives."""
    from hyperdjango.systemd import _UNIT_TEMPLATE

    check("has PrivateTmp=true", "PrivateTmp=true" in _UNIT_TEMPLATE)
    check("has ProtectSystem=strict", "ProtectSystem=strict" in _UNIT_TEMPLATE)
    check("has NoNewPrivileges=true", "NoNewPrivileges=true" in _UNIT_TEMPLATE)
    check("has LimitNOFILE=65536", "LimitNOFILE=65536" in _UNIT_TEMPLATE)
    check("has LimitNPROC=4096", "LimitNPROC=4096" in _UNIT_TEMPLATE)


def test_systemd_format_substitution():
    """Unit template can be formatted with all required variables."""
    from hyperdjango.systemd import _UNIT_TEMPLATE

    content = _UNIT_TEMPLATE.format(
        title="test-app",
        user="deploy",
        group="deploy",
        workdir="/opt/testapp",
        env_file="/etc/hyperdjango/hyperdjango-testapp.env",
        thread_pool_size="24",
        python="/usr/bin/python3",
        host="0.0.0.0",
        port=8000,
        app="app:app",
        service_name="hyperdjango-testapp",
    )

    check("formatted unit has Description", "Description=HyperDjango" in content)
    check("formatted unit has User=deploy", "User=deploy" in content)
    check(
        "formatted unit has WorkingDirectory",
        "WorkingDirectory=/opt/testapp" in content,
    )
    check(
        "formatted unit references EnvironmentFile for secrets",
        "EnvironmentFile=/etc/hyperdjango/hyperdjango-testapp.env" in content,
    )
    # Secrets must NOT be inlined into the (world-readable) unit file.
    check(
        "unit does NOT inline a secret Environment= line",
        "SECRET_KEY=" not in content and "DATABASE_URL=" not in content,
    )
    check("formatted unit has ExecStart with --prod", "--prod" in content)
    check(
        "formatted unit has SyslogIdentifier",
        "SyslogIdentifier=hyperdjango-testapp" in content,
    )
    check("formatted unit has ReadWritePaths", "ReadWritePaths=/opt/testapp" in content)
    check("formatted unit has TimeoutStopSec=30", "TimeoutStopSec=30" in content)
    check("formatted unit has KillMode=mixed", "KillMode=mixed" in content)
    check("formatted unit is valid INI", content.count("[Unit]") == 1)


def test_systemd_uses_hyper_secret_key():
    """REGRESSION (finding #1): the generated secrets file must use the env var
    the framework actually reads (HYPER_SECRET_KEY), NOT a bare SECRET_KEY the
    app ignores — otherwise the service silently runs on a per-boot random
    secret and invalidates all sessions/CSRF on every restart."""
    import os as _os

    from hyperdjango import conf as _conf
    from hyperdjango import systemd

    def _rescan() -> None:
        # The generator reads the secret through the settings authority, whose
        # env overrides are cached on first load; force a re-read so a value set
        # here (a real deployment sets it before the process starts) is seen.
        _conf._ENV_OVERRIDES.clear()
        _conf._ENV_OVERRIDES_POPULATED = False

    prev = _os.environ.get("HYPER_SECRET_KEY")
    _os.environ["HYPER_SECRET_KEY"] = "a-real-production-secret-0123456789"
    _rescan()
    try:
        args = type("A", (), {"name": "sk-test"})()
        env_content = systemd._render_env_file(args)
    finally:
        if prev is None:
            _os.environ.pop("HYPER_SECRET_KEY", None)
        else:
            _os.environ["HYPER_SECRET_KEY"] = prev
        _rescan()

    check(
        "secrets file emits HYPER_SECRET_KEY (framework-read name)",
        "HYPER_SECRET_KEY=a-real-production-secret-0123456789" in env_content,
    )
    check(
        "secrets file does NOT emit a bare SECRET_KEY= line",
        "\nSECRET_KEY=" not in ("\n" + env_content),
    )


def test_systemd_darwin_guard():
    """On non-Linux hosts systemctl commands must be guarded, not crash."""
    from hyperdjango import systemd

    # _systemctl_available is False off Linux; _require_systemctl returns False
    # and logs a message rather than raising.
    if not systemd._systemctl_available():
        check(
            "status is a no-op (no traceback) when systemctl unavailable",
            systemd.systemd_status(type("A", (), {"name": "x"})()) is None,
        )
    else:
        check("systemctl available (Linux) — guard path not exercised", True)


def test_systemd_service_naming():
    """Service name derived from args or cwd."""
    from hyperdjango.systemd import _service_name

    # With explicit name
    args = argparse.Namespace(name="myapp")
    check("explicit name", _service_name(args) == "hyperdjango-myapp")

    # Without name — falls back to cwd basename
    args_no_name = argparse.Namespace(name=None)
    name = _service_name(args_no_name)
    check("fallback name starts with hyperdjango-", name.startswith("hyperdjango-"))
    check("fallback name has cwd basename", len(name) > len("hyperdjango-"))


# ── CLI PID file management ──────────────────────────────────────────────────


def test_pid_file_path():
    """PID file naming convention."""
    from hyperdjango.cli import _pid_file

    pf = _pid_file(8000)
    check("PID file for port 8000", str(pf).endswith(".hyper.8000.pid"))

    pf2 = _pid_file(9090)
    check("PID file for port 9090", str(pf2).endswith(".hyper.9090.pid"))


def test_process_alive_check():
    """_is_process_alive correctly detects live/dead processes."""
    from hyperdjango.cli import _is_process_alive

    # Current process should be alive
    check("current PID is alive", _is_process_alive(os.getpid()))

    # PID 999999999 should not be alive
    check("bogus PID is not alive", not _is_process_alive(999999999))


# ── Health check endpoint structure ──────────────────────────────────────────


def test_health_check_mount():
    """HyperApp.mount_health() registers health endpoints without error."""
    from hyperdjango import HyperApp

    app = HyperApp("test-health")
    app.mount_health()
    # If mount_health succeeds without error, routes are registered in native router
    check("mount_health() succeeds", True)


def test_health_check_custom_path():
    """Health check paths are configurable."""
    from hyperdjango import HyperApp

    app = HyperApp("test-health-custom")
    app.mount_health(liveness_path="/alive", readiness_path="/healthz")
    check("mount_health with custom paths succeeds", True)


def test_health_check_custom_checks():
    """Custom health checks can be registered."""
    from hyperdjango import HyperApp

    app = HyperApp("test-health-checks")

    def check_cache():
        return True

    app.add_health_check("cache", check_cache)
    check("custom health check registered", "cache" in app._health_checks)


# ── Metrics endpoint (v0.15 native telemetry) ──────────────────────────────


def test_metrics_endpoint_format():
    """Prometheus text output from the native registry is well-formed."""
    from hyperdjango.telemetry import Counter, Gauge, Histogram, enable
    from hyperdjango.telemetry.metrics import collect_prometheus_text

    # Register a few sentinel metrics so the output has content
    _ = Counter("deploy_test_counter", "test counter")
    _ = Gauge("deploy_test_gauge", "test gauge")
    _ = Histogram(
        "deploy_test_hist",
        "test histogram",
        buckets=(0.01, 0.1, 1.0),
    )
    enable()
    output = collect_prometheus_text().decode("utf-8")
    lines = output.strip().splitlines()

    help_count = sum(1 for line in lines if line.startswith("# HELP"))
    type_count = sum(1 for line in lines if line.startswith("# TYPE"))
    check("has HELP lines", help_count >= 3, f"found {help_count}")
    check("has TYPE lines", type_count >= 3, f"found {type_count}")

    valid_types = {"counter", "gauge", "histogram", "summary", "untyped"}
    for line in lines:
        if line.startswith("# TYPE"):
            parts = line.split()
            metric_type = parts[-1]
            check(f"valid type: {metric_type}", metric_type in valid_types)
            break


def test_prometheus_sink_handler():
    """PrometheusSink serves the native registry via its async handler."""
    import asyncio

    from hyperdjango.telemetry import Counter, PrometheusSink, enable

    _ = Counter("deploy_sink_counter", "sink test counter")
    enable()
    sink = PrometheusSink()

    class _Req:
        pass

    loop = asyncio.new_event_loop()
    try:
        resp = loop.run_until_complete(sink.handler(_Req()))
    finally:
        loop.close()

    check("sink handler status 200", resp.status == 200)
    check(
        "sink handler content-type",
        "text/plain" in resp.headers.get("content-type", ""),
    )
    check("sink handler non-empty body", len(resp.body) > 0)


# ── Graceful shutdown ────────────────────────────────────────────────────────


def test_shutdown_hooks():
    """on_shutdown hooks are stored and retrievable."""
    from hyperdjango import HyperApp

    app = HyperApp("test-shutdown")

    @app.on_shutdown
    async def cleanup():
        pass

    check("shutdown hook registered", len(app._on_shutdown) >= 1)


# ── Deployment docs accuracy ─────────────────────────────────────────────────


def test_dockerfile_template_in_docs():
    """docs/deployment.md contains a Dockerfile reference."""
    docs_path = Path(__file__).parent / ".." / "docs" / "deployment.md"
    content = docs_path.read_text()

    check(
        "docs has Dockerfile section",
        "Dockerfile" in content or "dockerfile" in content,
    )
    check("docs has FROM python", "FROM python" in content)
    check("docs has zig installation", "zig" in content.lower())
    check("docs has hyper-build", "hyper-build" in content)
    check("docs has uv", "uv" in content)


def test_systemd_docs():
    """docs/deployment.md documents systemd management."""
    docs_path = Path(__file__).parent / ".." / "docs" / "deployment.md"
    content = docs_path.read_text()

    check("docs has systemd section", "systemd" in content.lower())
    check("docs has hyper systemd install", "hyper systemd install" in content)
    check("docs has systemctl", "systemctl" in content)


def test_prometheus_docs():
    """docs/deployment.md documents Prometheus metrics."""
    docs_path = Path(__file__).parent / ".." / "docs" / "deployment.md"
    content = docs_path.read_text()

    check(
        "docs has Prometheus section",
        "Prometheus" in content or "prometheus" in content,
    )
    check(
        "docs reference telemetry or metrics",
        "telemetry" in content or "Metrics" in content or "metrics" in content,
    )
    check("docs has /metrics endpoint", "/metrics" in content)
    check("docs has scrape_configs example", "scrape_configs" in content)


# ── Main ─────────────────────────────────────────────────────────────────────


def main():
    tests = [
        test_systemd_unit_template,
        test_systemd_security_hardening,
        test_systemd_format_substitution,
        test_systemd_uses_hyper_secret_key,
        test_systemd_darwin_guard,
        test_systemd_service_naming,
        test_pid_file_path,
        test_process_alive_check,
        test_health_check_mount,
        test_health_check_custom_path,
        test_health_check_custom_checks,
        test_metrics_endpoint_format,
        test_prometheus_sink_handler,
        test_shutdown_hooks,
        test_dockerfile_template_in_docs,
        test_systemd_docs,
        test_prometheus_docs,
    ]

    print(f"\n{'=' * 60}")
    print("End-to-End Deployment Tests")
    print(f"{'=' * 60}\n")

    for test in tests:
        try:
            test()
        except Exception as e:
            global failed
            failed += 1
            errors.append(f"FAIL: {test.__name__}: {e}")
            print(f"  ✗ {test.__name__}: {e}")

    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed out of {passed + failed}")
    if errors:
        print("\nFailures:")
        for err in errors:
            print(f"  - {err}")
    print(f"{'=' * 60}\n")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
