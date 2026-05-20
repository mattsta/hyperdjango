"""
Settings-driven telemetry bootstrap tests (P4.6).

# hyper-test: unit

Coverage:

    1.  TELEMETRY_ENABLED=False → configure_from_settings() returns None
    2.  TELEMETRY_ENABLED=True  → middleware + sinks instantiated
    3.  TELEMETRY_SINKS list parsed and deduplicated
    4.  TELEMETRY_SAMPLE_RATIO float honored
    5.  TELEMETRY_DRAIN_INTERVAL float honored
    6.  TELEMETRY_EXTRACT_TRACEPARENT bool honored
    7.  Unknown sink name → ValueError
    8.  Bootstrap integrates with a minimal app (use + on_shutdown)
    9.  SettingDefinitions registered: TELEMETRY_* in SETTING_DEFINITIONS
    10. DEFAULTS populated for every telemetry setting
"""

import sys
from unittest.mock import patch

from hyperdjango.conf import DEFAULTS, SETTING_DEFINITIONS, get_setting
from hyperdjango.telemetry import (
    InMemorySink,
    PrometheusSink,
    TelemetryBootstrap,
    configure_from_settings,
    disable,
)
from hyperdjango.telemetry.setup import _build_sinks

passed = 0
failed = 0
errors: list[str] = []


def check(name: str, cond: bool, msg: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        err = f"FAIL: {name}"
        if msg:
            err += f" — {msg}"
        errors.append(err)
        print(f"  {err}")


# ── Minimal app stub (matches HyperApp.use + HyperApp.on_shutdown shape) ───


class _AppStub:
    def __init__(self) -> None:
        self.middlewares: list = []
        self.shutdown_hooks: list = []

    def use(self, middleware) -> None:
        self.middlewares.append(middleware)

    def on_shutdown(self, hook) -> None:
        self.shutdown_hooks.append(hook)


# ── Tests ──────────────────────────────────────────────────────────────────


def test_disabled_returns_none() -> None:
    print("\n── TELEMETRY_ENABLED=False → None ──")
    with patch.dict(DEFAULTS, {"TELEMETRY_ENABLED": False}):
        result = configure_from_settings()
    check("returns None", result is None)


def test_enabled_builds_middleware() -> None:
    print("\n── TELEMETRY_ENABLED=True → bootstrap built ──")
    overrides = {
        "TELEMETRY_ENABLED": True,
        "TELEMETRY_SERVICE_NAME": "test_app",
        "TELEMETRY_SAMPLE_RATIO": 0.5,
        "TELEMETRY_DRAIN_INTERVAL": 0.05,
        "TELEMETRY_EXTRACT_TRACEPARENT": True,
        "TELEMETRY_SINKS": ["prometheus", "memory"],
    }
    bootstrap = None
    try:
        with patch.dict(DEFAULTS, overrides):
            bootstrap = configure_from_settings()
        check("returns TelemetryBootstrap", isinstance(bootstrap, TelemetryBootstrap))
        assert bootstrap is not None
        check("2 sinks wired", len(bootstrap.sinks) == 2)
        check(
            "prometheus_sink present",
            isinstance(bootstrap.prometheus_sink, PrometheusSink),
        )
        check("memory_sink present", isinstance(bootstrap.memory_sink, InMemorySink))
        check("stdout_sink absent", bootstrap.stdout_sink is None)
        check(
            "middleware drain interval honored",
            bootstrap.middleware.drain_interval_seconds == 0.05,
        )
        check(
            "tracer name set",
            bootstrap.middleware.tracer.name == "test_app",
        )
        check(
            "extract_traceparent forwarded",
            bootstrap.middleware.extract_traceparent is True,
        )
    finally:
        if bootstrap is not None:
            bootstrap.middleware.shutdown()
        disable()


def test_sinks_dedup() -> None:
    print("\n── Duplicate sink names deduplicated ──")
    sinks, prom, stdout, memory = _build_sinks(["prometheus", "prometheus", "stdout"])
    check("2 unique sinks", len(sinks) == 2)
    check("prometheus instantiated", prom is not None)
    check("stdout instantiated", stdout is not None)
    check("memory not instantiated", memory is None)


def test_unknown_sink_raises() -> None:
    print("\n── Unknown sink name → ValueError ──")
    try:
        _build_sinks(["prometheus", "datadog_hosted"])
        check("raised ValueError", False, "expected raise")
    except ValueError as exc:
        check("ValueError raised", True)
        check("error message mentions valid sinks", "prometheus" in str(exc))


def test_app_integration() -> None:
    print("\n── configure_from_settings(app) → use + on_shutdown ──")
    overrides = {
        "TELEMETRY_ENABLED": True,
        "TELEMETRY_SINKS": ["memory"],
        "TELEMETRY_DRAIN_INTERVAL": 0.05,
    }
    app = _AppStub()
    bootstrap = None
    try:
        with patch.dict(DEFAULTS, overrides):
            bootstrap = configure_from_settings(app)
        assert bootstrap is not None
        check("app.use called", len(app.middlewares) == 1)
        check(
            "middleware is the returned one", app.middlewares[0] is bootstrap.middleware
        )
        check("shutdown hook registered", len(app.shutdown_hooks) == 1)
        check(
            "shutdown hook is middleware.shutdown",
            app.shutdown_hooks[0] == bootstrap.middleware.shutdown,
        )
    finally:
        if bootstrap is not None:
            bootstrap.middleware.shutdown()
        disable()


def test_extract_traceparent_false_honored() -> None:
    print("\n── TELEMETRY_EXTRACT_TRACEPARENT=False honored ──")
    overrides = {
        "TELEMETRY_ENABLED": True,
        "TELEMETRY_SINKS": ["memory"],
        "TELEMETRY_DRAIN_INTERVAL": 0.05,
        "TELEMETRY_EXTRACT_TRACEPARENT": False,
    }
    bootstrap = None
    try:
        with patch.dict(DEFAULTS, overrides):
            bootstrap = configure_from_settings()
        assert bootstrap is not None
        check(
            "extract_traceparent=False forwarded",
            bootstrap.middleware.extract_traceparent is False,
        )
    finally:
        if bootstrap is not None:
            bootstrap.middleware.shutdown()
        disable()


def test_setting_definitions_registered() -> None:
    print("\n── SETTING_DEFINITIONS has telemetry entries ──")
    required = [
        "TELEMETRY_ENABLED",
        "TELEMETRY_SERVICE_NAME",
        "TELEMETRY_SAMPLE_RATIO",
        "TELEMETRY_DRAIN_INTERVAL",
        "TELEMETRY_EXTRACT_TRACEPARENT",
        "TELEMETRY_SINKS",
        "TELEMETRY_SPAN_RING_CAPACITY",
    ]
    for name in required:
        check(f"{name} in SETTING_DEFINITIONS", name in SETTING_DEFINITIONS)
        check(f"{name} in DEFAULTS", name in DEFAULTS)


def test_get_setting_defaults() -> None:
    print("\n── get_setting returns telemetry defaults ──")
    check(
        "TELEMETRY_ENABLED default is False", get_setting("TELEMETRY_ENABLED") is False
    )
    check(
        "TELEMETRY_SERVICE_NAME default",
        get_setting("TELEMETRY_SERVICE_NAME") == "hyperdjango",
    )
    check(
        "TELEMETRY_SAMPLE_RATIO default", get_setting("TELEMETRY_SAMPLE_RATIO") == 0.01
    )
    check(
        "TELEMETRY_DRAIN_INTERVAL default",
        get_setting("TELEMETRY_DRAIN_INTERVAL") == 1.0,
    )
    check(
        "TELEMETRY_EXTRACT_TRACEPARENT default",
        get_setting("TELEMETRY_EXTRACT_TRACEPARENT") is True,
    )
    check("TELEMETRY_SINKS default", get_setting("TELEMETRY_SINKS") == ["prometheus"])
    check(
        "TELEMETRY_SPAN_RING_CAPACITY default",
        get_setting("TELEMETRY_SPAN_RING_CAPACITY") == 16384,
    )


def main() -> int:
    print("=" * 70)
    print("  Telemetry settings bootstrap (P4.6)")
    print("=" * 70)

    test_setting_definitions_registered()
    test_get_setting_defaults()
    test_disabled_returns_none()
    test_sinks_dedup()
    test_unknown_sink_raises()
    test_enabled_builds_middleware()
    test_app_integration()
    test_extract_traceparent_false_honored()

    print()
    print("=" * 70)
    total = passed + failed
    print(f"Results: {passed}/{total} passed, {failed} failed")
    if errors:
        print("\nFailures:")
        for e in errors:
            print(f"  {e}")
    print("=" * 70)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
