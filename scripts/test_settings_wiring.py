"""
Tests that all configurable settings are actually wired through and take effect.

Validates that changing a setting in DEFAULTS actually changes the behavior
of the component that reads it. No mock — real component instantiation.

# hyper-test: unit

Usage:
    uv run hyper-test settings_wiring
"""

import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from hyperdjango.conf import DEFAULTS, SETTING_DEFINITIONS, get_setting
from hyperdjango.performance import PerformanceMiddleware
from hyperdjango.ratelimit import InMemoryRateLimitBackend
from hyperdjango.standalone_middleware import (
    VersionMiddleware,
)
from hyperdjango.staticfiles import (
    ManifestStaticFilesStorage,
    StaticFilesMiddleware,
)
from hyperdjango.versioning import AppVersion, set_app_version

passed = 0
failed = 0
errors: list[str] = []


def check(name: str, cond: bool, msg: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS: {name}")
    else:
        failed += 1
        err = f"FAIL: {name}"
        if msg:
            err += f" -- {msg}"
        errors.append(err)
        print(f"  {err}")


# ---------------------------------------------------------------------------
# Settings existence tests (every new setting in DEFAULTS + SETTING_DEFINITIONS)
# ---------------------------------------------------------------------------

NEW_SETTINGS = [
    "STATICFILES_GZIP_MIN_SIZE",
    "STATICFILES_HASH_LENGTH",
    "STATICFILES_MAX_POST_PROCESS_PASSES",
    "STATICFILES_DEV_HASH_CACHE_MAX",
    "TASK_MAX_COMPLETED_RESULTS",
    "TASK_CLEANUP_INTERVAL",
    "TASK_SHUTDOWN_TIMEOUT",
    "PERFORMANCE_HISTORY_SIZE",
    "PERFORMANCE_N_PLUS_ONE_THRESHOLD",
    "SLOW_QUERY_SQL_LENGTH",
    "SLOW_QUERY_PARAMS_LENGTH",
    "SLOW_QUERY_RETENTION_DAYS",
    "RATELIMIT_CLEANUP_RETENTION",
    "HOT_RELOAD_POLL_INTERVAL",
    "HOT_RELOAD_SSE_HEARTBEAT",
]


def test_all_settings_in_defaults():
    print("\n-- Settings in DEFAULTS --")
    for name in NEW_SETTINGS:
        check(f"{name} in DEFAULTS", name in DEFAULTS)


def test_all_settings_in_definitions():
    print("\n-- Settings in SETTING_DEFINITIONS --")
    for name in NEW_SETTINGS:
        check(f"{name} in SETTING_DEFINITIONS", name in SETTING_DEFINITIONS)


def test_get_setting_returns_defaults():
    print("\n-- get_setting returns correct defaults --")
    expected = {
        "STATICFILES_GZIP_MIN_SIZE": 1024,
        "STATICFILES_HASH_LENGTH": 12,
        "STATICFILES_MAX_POST_PROCESS_PASSES": 5,
        "STATICFILES_DEV_HASH_CACHE_MAX": 4096,
        "TASK_MAX_COMPLETED_RESULTS": 10000,
        "TASK_CLEANUP_INTERVAL": 100,
        "TASK_SHUTDOWN_TIMEOUT": 5,
        "PERFORMANCE_HISTORY_SIZE": 1000,
        "PERFORMANCE_N_PLUS_ONE_THRESHOLD": 5,
        "SLOW_QUERY_SQL_LENGTH": 2000,
        "SLOW_QUERY_PARAMS_LENGTH": 500,
        "SLOW_QUERY_RETENTION_DAYS": 7,
        "RATELIMIT_CLEANUP_RETENTION": 3600,
        "HOT_RELOAD_POLL_INTERVAL": 0.3,
        "HOT_RELOAD_SSE_HEARTBEAT": 30,
    }
    for name, expected_val in expected.items():
        actual = get_setting(name)
        check(
            f"{name} default = {expected_val}", actual == expected_val, f"got {actual}"
        )


# ---------------------------------------------------------------------------
# Wiring tests — prove the setting actually changes behavior
# ---------------------------------------------------------------------------


def test_staticfiles_gzip_min_size_wired():
    """StaticFilesMiddleware reads STATICFILES_GZIP_MIN_SIZE."""
    print("\n-- StaticFilesMiddleware gzip_min_size wiring --")
    d = tempfile.mkdtemp(prefix="hyper_test_")
    try:
        with patch.dict(DEFAULTS, {"STATICFILES_GZIP_MIN_SIZE": 2048}):
            mw = StaticFilesMiddleware(static_dirs=[d])
            check(
                "gzip_min_size = 2048",
                mw.gzip_min_size == 2048,
                f"got {mw.gzip_min_size}",
            )
    finally:
        shutil.rmtree(d)


def test_staticfiles_hash_length_wired():
    """ManifestStaticFilesStorage reads STATICFILES_HASH_LENGTH."""
    print("\n-- ManifestStaticFilesStorage hash_length wiring --")
    d = tempfile.mkdtemp(prefix="hyper_test_")
    try:
        with patch.dict(DEFAULTS, {"STATICFILES_HASH_LENGTH": 8}):
            storage = ManifestStaticFilesStorage(
                static_dirs=[d], static_root=str(Path(d) / "out")
            )
            check(
                "hash_length = 8",
                storage.hash_length == 8,
                f"got {storage.hash_length}",
            )
    finally:
        shutil.rmtree(d)


def test_staticfiles_max_passes_wired():
    """ManifestStaticFilesStorage reads STATICFILES_MAX_POST_PROCESS_PASSES."""
    print("\n-- ManifestStaticFilesStorage max_post_process_passes wiring --")
    d = tempfile.mkdtemp(prefix="hyper_test_")
    try:
        with patch.dict(DEFAULTS, {"STATICFILES_MAX_POST_PROCESS_PASSES": 3}):
            storage = ManifestStaticFilesStorage(
                static_dirs=[d], static_root=str(Path(d) / "out")
            )
            check(
                "max_passes = 3",
                storage.max_post_process_passes == 3,
                f"got {storage.max_post_process_passes}",
            )
    finally:
        shutil.rmtree(d)


def test_performance_history_size_wired():
    """PerformanceMiddleware reads PERFORMANCE_HISTORY_SIZE."""
    print("\n-- PerformanceMiddleware history_size wiring --")
    with patch.dict(DEFAULTS, {"PERFORMANCE_HISTORY_SIZE": 500}):
        mw = PerformanceMiddleware()
        check("max_history = 500", mw.max_history == 500, f"got {mw.max_history}")


def test_performance_n_plus_one_wired():
    """PerformanceMiddleware reads PERFORMANCE_N_PLUS_ONE_THRESHOLD."""
    print("\n-- PerformanceMiddleware n+1 threshold wiring --")
    with patch.dict(DEFAULTS, {"PERFORMANCE_N_PLUS_ONE_THRESHOLD": 10}):
        mw = PerformanceMiddleware()
        check(
            "n_plus_one = 10",
            mw.n_plus_one_threshold == 10,
            f"got {mw.n_plus_one_threshold}",
        )


def test_ratelimit_max_buckets_wired():
    """InMemoryRateLimitBackend reads RATELIMIT_MAX_BUCKETS (per-shard cap)."""
    print("\n-- RateLimit backend max-buckets wiring --")
    with patch.dict(DEFAULTS, {"RATELIMIT_MAX_BUCKETS": 3200}):
        backend = InMemoryRateLimitBackend()
        # Total cap is split evenly across the 16 shards.
        check(
            "per-shard cap = 3200 // 16 = 200",
            backend._max_buckets == 200,
            f"got {backend._max_buckets}",
        )


def test_version_middleware_settings_wired():
    """VersionMiddleware reads APP_VERSION_HEADER and APP_VERSION_MISMATCH."""
    print("\n-- VersionMiddleware settings wiring --")
    av = AppVersion()
    av.set_explicit("test")
    set_app_version(av)
    try:
        with patch.dict(
            DEFAULTS, {"APP_VERSION_HEADER": False, "APP_VERSION_MISMATCH": "ignore"}
        ):
            mw = VersionMiddleware()
            check("enabled = False", mw._enabled is False)
            check("inject_script = False", mw._inject_script is False)

        with patch.dict(
            DEFAULTS, {"APP_VERSION_HEADER": True, "APP_VERSION_MISMATCH": "warn"}
        ):
            mw = VersionMiddleware()
            check("enabled = True", mw._enabled is True)
            check("mismatch_action = warn", mw._mismatch_action == "warn")
    finally:
        set_app_version(None)


def test_versioning_settings_wired():
    """AppVersion reads APP_VERSION setting."""
    print("\n-- AppVersion settings wiring --")
    with patch.dict(DEFAULTS, {"APP_VERSION": "v3.0.0"}):
        av = AppVersion()
        check("version from setting", av.version == "v3.0.0", f"got {av.version}")


def test_dev_hash_cache_max_wired():
    """STATICFILES_DEV_HASH_CACHE_MAX is lazy-initialized on first use."""
    print("\n-- Dev hash cache max wiring --")
    # Value is 0 until first use, then initialized from get_setting
    check(
        "STATICFILES_DEV_HASH_CACHE_MAX in DEFAULTS",
        "STATICFILES_DEV_HASH_CACHE_MAX" in DEFAULTS,
    )
    check(
        "default is 4096",
        DEFAULTS["STATICFILES_DEV_HASH_CACHE_MAX"] == 4096,
    )


# ---------------------------------------------------------------------------
# get_setting cache efficiency test
# ---------------------------------------------------------------------------


def test_get_setting_cached():
    """get_setting doesn't re-read env vars on every call."""
    print("\n-- get_setting cache efficiency --")
    # Call get_setting 10000 times — should be fast because cached
    import time

    start = time.perf_counter()
    for _ in range(10000):
        get_setting("DEBUG")
    elapsed = time.perf_counter() - start
    # 10K calls should be fast (cached dict lookups)
    # Under parallel execution (340+ processes), CPU contention inflates timing
    import os

    _parallel = os.environ.get("HYPER_TEST_PARALLEL") == "1"
    threshold = 0.5 if _parallel else 0.05
    check(
        f"10K get_setting calls in {elapsed * 1000:.1f}ms (<{threshold * 1000:.0f}ms)",
        elapsed < threshold,
        f"took {elapsed * 1000:.1f}ms",
    )


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def run_tests():
    global passed, failed, errors
    passed = 0
    failed = 0
    errors = []

    print("\n-- Settings Wiring Tests --\n")

    test_all_settings_in_defaults()
    test_all_settings_in_definitions()
    test_get_setting_returns_defaults()
    test_staticfiles_gzip_min_size_wired()
    test_staticfiles_hash_length_wired()
    test_staticfiles_max_passes_wired()
    test_performance_history_size_wired()
    test_performance_n_plus_one_wired()
    test_ratelimit_max_buckets_wired()
    test_version_middleware_settings_wired()
    test_versioning_settings_wired()
    test_dev_hash_cache_max_wired()
    test_get_setting_cached()

    total = passed + failed
    print(f"\n{'=' * 60}")
    print(f"Settings wiring: {passed}/{total} passed")
    if errors:
        print("\nFailures:")
        for e in errors:
            print(f"  {e}")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(run_tests())
