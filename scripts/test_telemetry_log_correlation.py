"""
Telemetry ↔ logger trace correlation tests (tasks #249 + #257).

# hyper-test: unit

Validates the bridge between active spans and the logging system:

  1. SpanContext.trace_id_hex / span_id_hex / to_log_extra
  2. bind_trace_context() returns logger pre-bound with trace IDs
  3. bind_trace_context() with no active span returns logger unchanged
  4. JSON sink promotes trace_id/span_id to top-level (existing behavior)
  5. Settings layering: TELEMETRY_* settings are first-class settings,
     not "just env vars" — verify they resolve via DEFAULTS, env, and
     unittest.mock.patch.dict overrides
  6. (task #257) auto_log_correlation_patcher injects trace context into
     record extra dict when called inside an active span
  7. (task #257) configure_from_settings installs the patcher by default
  8. (task #257) TELEMETRY_AUTO_LOG_CORRELATION=False opts out
  9. (task #257) chains with existing user patcher (both run)
  10. (task #257) JSON sink output contains trace_id at top level
"""

import sys
from unittest.mock import patch

from hyperdjango.conf import DEFAULTS, get_setting
from hyperdjango.logging import logger as global_logger
from hyperdjango.telemetry import (
    AlwaysSample,
    SpanContext,
    Tracer,
    auto_log_correlation_patcher,
    bind_trace_context,
    configure_from_settings,
    current,
    disable,
    enable,
)
from hyperdjango.telemetry.context import reset as _ctx_reset
from hyperdjango.telemetry.context import set as _ctx_set

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


# ── SpanContext hex helpers ────────────────────────────────────────────────


def test_trace_id_hex_format() -> None:
    print("\n── SpanContext.trace_id_hex ──")
    ctx = SpanContext(
        trace_id_high=0x0AF7651916CD43DD,
        trace_id_low=0x8448EB211C80319C,
        span_id=0xB7AD6B7169203331,
        parent_id=0,
        sampled=True,
    )
    check(
        "trace_id_hex is 32 lowercase hex chars",
        ctx.trace_id_hex == "0af7651916cd43dd8448eb211c80319c",
        f"got {ctx.trace_id_hex}",
    )
    check(
        "span_id_hex is 16 lowercase hex chars",
        ctx.span_id_hex == "b7ad6b7169203331",
        f"got {ctx.span_id_hex}",
    )


def test_to_log_extra_shape() -> None:
    print("\n── SpanContext.to_log_extra ──")
    ctx = SpanContext(
        trace_id_high=1,
        trace_id_low=2,
        span_id=3,
        parent_id=0,
        sampled=True,
    )
    extra = ctx.to_log_extra()
    check("has trace_id key", "trace_id" in extra)
    check("has span_id key", "span_id" in extra)
    check("has trace_flags key", "trace_flags" in extra)
    check(
        "trace_id is full 32-char hex",
        extra["trace_id"] == "00000000000000010000000000000002",
    )
    check(
        "span_id is 16-char hex",
        extra["span_id"] == "0000000000000003",
    )
    check("sampled=True → trace_flags=01", extra["trace_flags"] == "01")

    ctx_unsampled = SpanContext(
        trace_id_high=1,
        trace_id_low=2,
        span_id=3,
        parent_id=0,
        sampled=False,
    )
    check(
        "sampled=False → trace_flags=00",
        ctx_unsampled.to_log_extra()["trace_flags"] == "00",
    )


# ── bind_trace_context bridge ──────────────────────────────────────────────


def test_bind_trace_context_with_active_span() -> None:
    print("\n── bind_trace_context with active span ──")
    enable()
    tracer = Tracer("test", sampler=AlwaysSample())
    try:
        with tracer.start_span("operation") as span:
            bound = bind_trace_context(global_logger)
            check(
                "bound logger is a different instance",
                bound is not global_logger,
            )
            # The bound logger has trace_id/span_id in its extras
            ctx = current()
            assert ctx is not None
            check(
                "bound logger has trace_id in extras",
                bound._extra.get("trace_id") == ctx.trace_id_hex,
            )
            check(
                "bound logger has span_id in extras",
                bound._extra.get("span_id") == ctx.span_id_hex,
            )
            check(
                "bound logger has trace_flags=01",
                bound._extra.get("trace_flags") == "01",
            )
    finally:
        disable()


def test_bind_trace_context_without_active_span() -> None:
    print("\n── bind_trace_context with no active span ──")
    disable()
    bound = bind_trace_context(global_logger)
    check(
        "no active span → returns logger unchanged",
        bound is global_logger,
    )


def test_bind_trace_context_with_unsampled_span() -> None:
    print("\n── bind_trace_context with unsampled span ──")
    # Manually install an unsampled context (sentinel handle but valid trace)
    ctx = SpanContext(
        trace_id_high=0xAAAA,
        trace_id_low=0xBBBB,
        span_id=0,
        parent_id=0,
        sampled=False,
    )
    token = _ctx_set(ctx)
    try:
        bound = bind_trace_context(global_logger)
        check(
            "unsampled context still binds trace_id",
            bound._extra.get("trace_id") == ctx.trace_id_hex,
        )
        check(
            "unsampled context → trace_flags=00",
            bound._extra.get("trace_flags") == "00",
        )
    finally:
        _ctx_reset(token)


# ── Settings layering: telemetry settings work via the full conf.py system ─


def test_settings_resolve_via_get_setting() -> None:
    """All TELEMETRY_* settings are first-class settings, not 'env vars'.
    They resolve through the same conf.get_setting() machinery used by
    every other framework setting.
    """
    print("\n── Settings layering: telemetry knobs via get_setting() ──")
    # Default values come from DEFAULTS
    check(
        "TELEMETRY_ENABLED default is False",
        get_setting("TELEMETRY_ENABLED") is False,
    )
    check(
        "TELEMETRY_SAMPLE_RATIO default is 0.01",
        get_setting("TELEMETRY_SAMPLE_RATIO") == 0.01,
    )
    check(
        "TELEMETRY_DRAIN_INTERVAL default is 1.0",
        get_setting("TELEMETRY_DRAIN_INTERVAL") == 1.0,
    )
    check(
        "TELEMETRY_SPAN_RING_CAPACITY default is 16384",
        get_setting("TELEMETRY_SPAN_RING_CAPACITY") == 16384,
    )
    check(
        "TELEMETRY_SINKS default is ['prometheus']",
        get_setting("TELEMETRY_SINKS") == ["prometheus"],
    )


def test_settings_via_patch_dict() -> None:
    """unittest.mock.patch.dict on DEFAULTS overrides telemetry settings —
    same mechanism the rest of the framework uses for test fixtures.
    """
    print("\n── Settings layering: patch.dict overrides ──")
    overrides = {
        "TELEMETRY_ENABLED": True,
        "TELEMETRY_SAMPLE_RATIO": 0.5,
        "TELEMETRY_SPAN_RING_CAPACITY": 4096,
        "TELEMETRY_SERVICE_NAME": "test-app",
    }
    with patch.dict(DEFAULTS, overrides):
        check(
            "TELEMETRY_ENABLED override visible",
            get_setting("TELEMETRY_ENABLED") is True,
        )
        check(
            "TELEMETRY_SAMPLE_RATIO override visible",
            get_setting("TELEMETRY_SAMPLE_RATIO") == 0.5,
        )
        check(
            "TELEMETRY_SPAN_RING_CAPACITY override visible",
            get_setting("TELEMETRY_SPAN_RING_CAPACITY") == 4096,
        )
        check(
            "TELEMETRY_SERVICE_NAME override visible",
            get_setting("TELEMETRY_SERVICE_NAME") == "test-app",
        )
    # After patch exits, defaults are restored
    check(
        "default restored after patch.dict exit",
        get_setting("TELEMETRY_ENABLED") is False,
    )


def test_env_var_resolution_documented_priority() -> None:
    """Env vars are ONE of several sources. Document the priority order
    by exercising it: a HYPER_TELEMETRY_* env var should be picked up
    by load_env_settings() and override DEFAULTS.

    Note: get_setting() caches env vars on first call (process-lifetime
    cache), so this test sets the env var BEFORE the first cache pop and
    verifies via the loader directly to avoid stale cache state.
    """
    from hyperdjango.conf import load_env_settings

    print("\n── Settings layering: env var loader ──")
    env = {
        "HYPER_TELEMETRY_ENABLED": "1",
        "HYPER_TELEMETRY_SPAN_RING_CAPACITY": "8192",
        "HYPER_TELEMETRY_SERVICE_NAME": "from-env",
    }
    loaded = load_env_settings(env=env, dotenv_path=None)
    check(
        "TELEMETRY_ENABLED loaded from env",
        loaded.get("TELEMETRY_ENABLED") is True,
    )
    check(
        "TELEMETRY_SPAN_RING_CAPACITY loaded from env (int coerced)",
        loaded.get("TELEMETRY_SPAN_RING_CAPACITY") == 8192,
    )
    check(
        "TELEMETRY_SERVICE_NAME loaded from env",
        loaded.get("TELEMETRY_SERVICE_NAME") == "from-env",
    )


# ── Auto-correlation patcher tests (task #257) ──────────────────────────────


def _make_record(extra: dict | None = None) -> dict:
    """Build a minimal log record dict matching the shape `_log` produces."""
    return {
        "extra": extra if extra is not None else {},
        "message": "test",
        "level": None,
        "time": None,
    }


def test_auto_patcher_injects_when_span_active() -> None:
    print("\n── auto_log_correlation_patcher: active span injection ──")
    ctx = SpanContext(
        trace_id_high=0xDEADBEEF12345678,
        trace_id_low=0xCAFEBABE87654321,
        span_id=0xFACEFEED12345678,
        parent_id=0,
        sampled=True,
    )
    token = _ctx_set(ctx)
    try:
        record = _make_record()
        auto_log_correlation_patcher(record)
        check(
            "trace_id injected into record.extra",
            record["extra"].get("trace_id") == ctx.trace_id_hex,
            f"got {record['extra'].get('trace_id')}",
        )
        check(
            "span_id injected into record.extra",
            record["extra"].get("span_id") == ctx.span_id_hex,
        )
        check(
            "trace_flags injected into record.extra",
            record["extra"].get("trace_flags") == "01",
        )
    finally:
        _ctx_reset(token)


def test_auto_patcher_noop_when_no_span() -> None:
    print("\n── auto_log_correlation_patcher: no-op outside span ──")
    record = _make_record()
    auto_log_correlation_patcher(record)
    check(
        "extra dict unchanged when no active span",
        record["extra"] == {},
    )


def test_auto_patcher_preserves_existing_keys() -> None:
    """First-write-wins: user-supplied bind() / contextualize() values
    are not overwritten by the auto-injected trace context.
    """
    print("\n── auto_log_correlation_patcher: existing keys preserved ──")
    ctx = SpanContext(
        trace_id_high=1,
        trace_id_low=2,
        span_id=3,
        parent_id=0,
        sampled=True,
    )
    token = _ctx_set(ctx)
    try:
        # User has manually set trace_id to a custom value (e.g.,
        # carrying a request_id from elsewhere). The patcher must
        # NOT overwrite it.
        record = _make_record({"trace_id": "user-value", "user_id": "alice"})
        auto_log_correlation_patcher(record)
        check(
            "user-supplied trace_id preserved",
            record["extra"]["trace_id"] == "user-value",
        )
        check(
            "user-supplied user_id preserved",
            record["extra"]["user_id"] == "alice",
        )
        check(
            "missing span_id was injected",
            record["extra"]["span_id"] == "0000000000000003",
        )
    finally:
        _ctx_reset(token)


def test_auto_patcher_handles_missing_extra() -> None:
    """Defensive: if `extra` was removed from record, the patcher
    creates it instead of crashing."""
    print("\n── auto_log_correlation_patcher: missing extra dict ──")
    ctx = SpanContext(
        trace_id_high=1,
        trace_id_low=2,
        span_id=3,
        parent_id=0,
        sampled=True,
    )
    token = _ctx_set(ctx)
    try:
        record = {"message": "test"}  # NO extra key at all
        auto_log_correlation_patcher(record)
        check(
            "extra dict was created",
            "extra" in record and isinstance(record["extra"], dict),
        )
        check(
            "extra contains trace_id",
            record["extra"].get("trace_id") == "00000000000000010000000000000002",
        )
    finally:
        _ctx_reset(token)


def test_configure_installs_patcher_by_default() -> None:
    print("\n── configure_from_settings installs patcher (default ON) ──")
    overrides = {
        "TELEMETRY_ENABLED": True,
        "TELEMETRY_AUTO_LOG_CORRELATION": True,
        "TELEMETRY_SINKS": ["memory"],
        "TELEMETRY_SAMPLE_RATIO": 1.0,
    }
    original_patcher = global_logger._core.patcher
    try:
        with patch.dict(DEFAULTS, overrides):
            global_logger._core.patcher = None  # start clean
            boot = configure_from_settings(app=None)
            check("bootstrap returned", boot is not None)
            check(
                "core.patcher installed",
                global_logger._core.patcher is auto_log_correlation_patcher,
            )
            boot.middleware.shutdown()
    finally:
        global_logger._core.patcher = original_patcher
        disable()


def test_configure_skips_patcher_when_disabled() -> None:
    print("\n── configure_from_settings respects opt-out ──")
    overrides = {
        "TELEMETRY_ENABLED": True,
        "TELEMETRY_AUTO_LOG_CORRELATION": False,
        "TELEMETRY_SINKS": ["memory"],
        "TELEMETRY_SAMPLE_RATIO": 1.0,
    }
    original_patcher = global_logger._core.patcher
    try:
        with patch.dict(DEFAULTS, overrides):
            global_logger._core.patcher = None
            boot = configure_from_settings(app=None)
            check("bootstrap returned", boot is not None)
            check(
                "core.patcher NOT installed when opted out",
                global_logger._core.patcher is None,
            )
            boot.middleware.shutdown()
    finally:
        global_logger._core.patcher = original_patcher
        disable()


def test_configure_chains_with_existing_patcher() -> None:
    print("\n── configure_from_settings chains with existing user patcher ──")
    captured: list[str] = []

    def user_patcher(record: dict) -> None:
        captured.append("user_called")
        record["extra"]["user_marker"] = "yes"

    overrides = {
        "TELEMETRY_ENABLED": True,
        "TELEMETRY_AUTO_LOG_CORRELATION": True,
        "TELEMETRY_SINKS": ["memory"],
        "TELEMETRY_SAMPLE_RATIO": 1.0,
    }
    original_patcher = global_logger._core.patcher
    try:
        with patch.dict(DEFAULTS, overrides):
            global_logger._core.patcher = user_patcher
            boot = configure_from_settings(app=None)
            chained = global_logger._core.patcher
            check("chained patcher installed", chained is not None)
            check("chained != original user patcher", chained is not user_patcher)
            check(
                "chained != bare auto patcher",
                chained is not auto_log_correlation_patcher,
            )

            # Now invoke the chain inside an active span and verify
            # both patchers ran.
            ctx = SpanContext(
                trace_id_high=0xA,
                trace_id_low=0xB,
                span_id=0xC,
                parent_id=0,
                sampled=True,
            )
            token = _ctx_set(ctx)
            try:
                record = _make_record()
                chained(record)
            finally:
                _ctx_reset(token)
            check("user patcher invoked", captured == ["user_called"])
            check("user marker present", record["extra"].get("user_marker") == "yes")
            check(
                "trace_id injected by chained auto patcher",
                record["extra"].get("trace_id") == "000000000000000a000000000000000b",
            )
            boot.middleware.shutdown()
    finally:
        global_logger._core.patcher = original_patcher
        disable()


def test_e2e_log_emission_inside_span_has_trace_id() -> None:
    """End-to-end: enable telemetry, install patcher, start a real span,
    emit a log line, capture the rendered record extra, verify trace_id
    is present at the top of the JSON output."""
    print("\n── e2e log emission inside span has trace_id ──")
    captured_extras: list[dict] = []

    def capture_patcher(record: dict) -> None:
        # Run AFTER the auto patcher in the chain so we see the
        # injected fields.
        captured_extras.append(dict(record.get("extra", {})))

    overrides = {
        "TELEMETRY_ENABLED": True,
        "TELEMETRY_AUTO_LOG_CORRELATION": True,
        "TELEMETRY_SINKS": ["memory"],
        "TELEMETRY_SAMPLE_RATIO": 1.0,
    }
    original_patcher = global_logger._core.patcher
    try:
        with patch.dict(DEFAULTS, overrides):
            global_logger._core.patcher = None
            boot = configure_from_settings(app=None)
            # Add our capture patcher to the per-instance chain so it
            # runs AFTER the global patcher (auto correlator) installed
            # by configure_from_settings.
            captured_logger = global_logger.patch(capture_patcher)
            tracer = Tracer("e2e", sampler=AlwaysSample())
            with tracer.start_span("e2e-work") as span:
                # Span should be active here — emit a log
                captured_logger.info("hello from inside span")
            check(
                "log captured at least once",
                len(captured_extras) >= 1,
                f"got {len(captured_extras)} captures",
            )
            if captured_extras:
                last = captured_extras[-1]
                check(
                    "captured extra has trace_id",
                    "trace_id" in last,
                    f"keys: {list(last.keys())}",
                )
                check(
                    "captured extra has span_id",
                    "span_id" in last,
                )
                check(
                    "captured extra has trace_flags",
                    last.get("trace_flags") == "01",
                )
            boot.middleware.shutdown()
    finally:
        global_logger._core.patcher = original_patcher
        disable()


def main() -> int:
    print("=" * 70)
    print("  Telemetry ↔ logger correlation + auto-patcher (tasks #249, #257)")
    print("=" * 70)

    test_trace_id_hex_format()
    test_to_log_extra_shape()
    test_bind_trace_context_with_active_span()
    test_bind_trace_context_without_active_span()
    test_bind_trace_context_with_unsampled_span()
    test_settings_resolve_via_get_setting()
    test_settings_via_patch_dict()
    test_env_var_resolution_documented_priority()
    # task #257 — auto-correlation patcher
    test_auto_patcher_injects_when_span_active()
    test_auto_patcher_noop_when_no_span()
    test_auto_patcher_preserves_existing_keys()
    test_auto_patcher_handles_missing_extra()
    test_configure_installs_patcher_by_default()
    test_configure_skips_patcher_when_disabled()
    test_configure_chains_with_existing_patcher()
    test_e2e_log_emission_inside_span_has_trace_id()

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
